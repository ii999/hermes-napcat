# NapCat 流式上传与 base64 直发

2026-09-30，延续 PR #3 的媒体策略与图片引用实现。此扩展复用已有正向/反向 OneBot WebSocket，不开第二个 HTTP 服务，不把任意远程 URL 交给 NapCat 下载，也不开放任意远端文件路径。

## 默认值与迁移

| 设置 | 本次默认值 | 作用 |
| --- | --- | --- |
| `media.max_bytes` | 32 MiB | 单项网络下载、本地图片和解码后的 inline 输入上限 |
| `media.inline_max_bytes` | 10 MiB | 可以直接装入一次发送请求的原始媒体字节上限 |
| `ws_max_bytes` | 16 MiB | 单次收/发 WebSocket 消息上限，包含 base64 和 UTF-8 JSON |
| `media.timeout` | 60 秒 | HTTP 媒体下载超时 |
| `media.streaming.max_bytes` | 256 MiB | 单项分块上传上限，同时受本地媒体/图片各自上限约束 |
| `media.streaming.chunk_bytes` | 256 KiB | 每块原始字节数；按实际 WS 预算自动减小 |
| `media.streaming.max_concurrent` | 1 | 同时上传数，避免 NapCat 合并阶段过高内存占用 |
| `event_queue_max_bytes` | 64 MiB | 消息队列及正在处理的入站消息的序列化字节预算 |
| `media.base64_batch_max_bytes` | 64 MiB | 一次 QQ 工具调用中 base64 来源的解码后总大小预算 |

本地普通文件/音视频仍受 `qq_tools.max_local_media_bytes`（默认 256 MiB）限制。网络音视频/文件先经下载，因此仍受 32 MiB 下载上限。base64 单项同时受 `media.base64_max_bytes`（默认 32 MiB）和 `media.max_bytes` 限制。每轮 4 个入站附件、8 个工具媒体项、512 MiB 缓存等原有数量/存储限制没有扩大。

已有 YAML 中的显式大小值继续生效，升级不会覆盖它们。省略的字段采用新默认值。如果保留旧的 2 MiB WS 上限而未填写 inline 上限，运行时会把实际内联预算减小，并对较大的媒体尝试分块上传，不会因新默认值导致配置校验失败。显式把缓存额度设得低于新下载上限时，仍须调低 `max_bytes` 或调整缓存额度，避免破坏操作者设置的磁盘上限。

公网 URL 模式和图片历史引用仍按原有规则显式启用；本次增大默认大小不改变网络白名单、SSRF 防护、群观察范围或工具授权。NapCat、代理和 QQ 也可能有各自限制，插件默认值不代表平台保证接受的大小。修改配置后重启 Gateway。

## 传输选择

URL、本地文件、受控图片引用和显式 base64 输入进入相同的权限/类型/大小检查。已映射文件优先使用共享路径，有 `shared_cache_dir` 时优先暂存到共享卷。没有共享暂存时，小文件使用 base64，超过实际 inline/WS 预算的文件通过 `upload_file_stream` 上传，再用当前连接返回的受控 NapCat 路径发送。

已有的 base64 小图无需落盘或整图重新编码：插件分块解码校验、检查图片文件头和 data URI 的 MIME，然后直接使用已验证的 base64 发送。较大的 base64 或需要共享暂存的输入会先解码到受配额约束的缓存，再走共享或流式路径。

普通图文多图消息继续按段保序拆分。合并转发卡片仍是一次发送，不自动拆卡；可通过 `streaming.mode: always` 将非共享媒体预先分块上传，使卡片内只携带短路径。不会在 QQ 消息发送失败后偷偷换传输方式重发。

基础 Gateway 的 `send_document` 也使用统一媒体发送路径，现在无共享卷时可直接使用 base64/流式传输。独立 cron sender 的媒体契约仍未实现。

## 配置

以下放在 `gateway.platforms.napcat.extra` 下，展示默认值，可只填写需要覆盖的字段：

```yaml
ws_max_bytes: 16777216
event_queue_max_bytes: 67108864
media:
  max_bytes: 33554432
  inline_max_bytes: 10485760
  timeout: 60
  base64_max_bytes: 33554432
  base64_batch_max_bytes: 67108864
  streaming:
    mode: auto                 # auto / disabled / always
    max_bytes: 268435456
    chunk_bytes: 262144
    timeout: 300               # 包括等待上传名额、校验、分块、合并
    chunk_timeout: 30
    finalize_timeout: 120
    max_concurrent: 1
    max_pending: 8             # 包括正在运行和等待中的上传
    file_retention_seconds: 900
```

`auto` 默认按共享、inline、stream 的顺序选择。`always` 在没有共享路径时跳过 inline，即使小图也上传分块。`disabled` 关闭流式回退，保留共享和 inline；超限时明确报错。插件不依赖容易过期的版本号猜测能力，不为不支持 `upload_file_stream` 的后端做无限探测。第一次需要流式上传而后端拒绝时，操作失败并提示检查兼容性或使用共享存储。

上传总超时之外，失败路径可能额外花最多 5 秒请求一次当前 stream 的 reset。关闭适配器会取消待处理/正在上传的任务。流式上传和普通 QQ send 使用不同的超时配置，不会为传大文件而放宽全部 QQ 动作的等待时间。

## base64 输入

Gateway 的图片、语音、视频、文档发送，以及 `qq_send_message`、`qq_send_media` 和自建合并转发节点的媒体来源支持：

```text
base64://<标准、完整、带正确 padding 的 base64>
data:image/png;base64,<标准 base64>
data:application/pdf;base64,<标准 base64>
```

不猜测无前缀的裸 base64，不接受 MIME 参数、任意编码、空数据、URL-safe 字母表、内嵌空白、非法字符、错误 padding 或非规范编码。图片支持现有的 PNG/JPEG/GIF/WebP 文件头；data URI 声明的图片 MIME 必须匹配实际文件头。音视频不做完整编解码验证或转码。

工具调用示意，省略号是说明占位，实际调用须传完整数据：

```json
{"media_type":"image","source":"data:image/png;base64,<完整数据>"}
```

```json
{"media_type":"file","file_name":"report.pdf","source":"base64://<完整数据>"}
```

没有指定 inline 文件名时，文件上传使用 `attachment.bin`；不会把整段 base64 当作文件名。URL/本地路径原有的 8192 字符限制继续生效，base64 输入采用解码后字节上限，并在工具 action key 序列化前检查整批预算。建议 Agent 使用本地路径或 `media:<id>`，无需让模型生成大段 base64 文本。

这里增加的是受控出站输入。入站消息和 `get_image` 返回的本地路径、base64 内容仍不会自动成为文件读取来源，防止把两个机器的路径与权限混为一谈。

## 完整性与失败语义

插件先对受控本地文件分块计算 SHA-256，再用随机 stream ID 和插件生成的随机文件名逐块上传。每块必须返回匹配的 stream ID、状态和计数；发送独立的 `is_complete` 后，插件还会核对总大小、SHA-256 和远端文件名。上传过程检查源文件是否改变，逐次 action 都绑定原连接 epoch。

NapCat 完成上传后仍未向 QQ 发送消息。只有完整性校验通过，插件才调用原来的消息或文件上传动作。断线、超时、错误计数、错误路径和校验失败会中止；不重放块、不跨连接续传、不自动退回大 base64。发送 QQ 时发生结果不确定，继续使用 `delivery_uncertain` 语义，不重新上传并再发一遍。

NapCat 返回的路径只作为该次上传的内部能力使用，不会在 Hermes 上打开。它必须是绝对路径，文件名必须等于本次随机生成的名称，不能包含父目录跳转、控制字符、UNC 或网络 URL。正式发送前还检查连接 epoch、远端文件保留期及原 `media_id` 的权限/撤回状态；路径过期或重连后拒绝使用。

失败时只对自己的随机 stream 发起一次 best-effort reset，不调用可删除任意远端文件的清理接口。所核验的 NapCat 实现即使 reset 成功也返回错误，所以插件不把 reset 回执当成确定的删除证明。未完成 stream 依赖 NapCat 自身的过期清理，完成文件按 `file_retention_seconds` 请求回收；完成回执丢失时也不无限保留文件。已发给 QQ 的媒体不会在发送后立即删除，以免破坏异步读取。

## 上游核验与验收范围

2026-09-30 核验 NapCatQQ `26d7533e0f5800fdff865ab2f2ad7692917e1076`：

- [上传实现](https://github.com/NapNeko/NapCatQQ/blob/26d7533e0f5800fdff865ab2f2ad7692917e1076/packages/napcat-onebot/action/stream/UploadFileStream.ts)
- [流式响应类型](https://github.com/NapNeko/NapCatQQ/blob/26d7533e0f5800fdff865ab2f2ad7692917e1076/packages/napcat-onebot/action/stream/StreamBasic.ts)
- [OneBot action 响应封装](https://github.com/NapNeko/NapCatQQ/blob/26d7533e0f5800fdff865ab2f2ad7692917e1076/packages/napcat-onebot/action/OneBotAction.ts)
- [上游 WebSocket 示例](https://github.com/NapNeko/NapCatQQ/blob/26d7533e0f5800fdff865ab2f2ad7692917e1076/packages/napcat-onebot/action/stream/test_upload_stream.py)

上游会在最终合并时把各块放入内存，磁盘分块不等于端到端恒定内存。因此默认上传并发为 1、上限 256 MiB，未宣称支持 TB 级文件或任意低内存机器。Linux/Windows 文件名处理使用跨平台路径检查，但尚未在 Windows QQ 环境执行。

本地与 CI 测试使用 loopback HTTP/WebSocket 和 Hermes/OneBot 替身，覆盖正反向传输、base64 输入、分块/哈希/错误响应、取消、排队、重连失效和不重发。没有实际启动用户的 NapCat、QQ 或完整 Hermes Gateway；部署时应检验 1 MiB/10 MiB/32 MiB 图片、较大普通文件、共享卷权限及发送期间断线。
