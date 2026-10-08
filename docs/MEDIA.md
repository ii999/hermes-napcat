# 媒体收发、理解与受控引用

图片、语音、视频和文件共用同会话引用、认证下载和自有缓存。收取字节、理解内容和向 QQ 发送是独立能力；下载成功不代表模型已经看过或听过附件。插件使用 Hermes 已安装的媒体处理接口，不修改 Hermes 核心。

## 升级和默认行为

升级 Python 包及目录插件入口后重启 Gateway。配置没有热加载。旧配置继续有效：未配置 `media.inbound` 或 `media.outbound` 的方向，继承旧的 `media.allowed_hosts`、`media.trusted_private_origins`，默认仍为 allowlist。显式配置某个方向后，该方向使用自己的完整策略和默认值，不与旧列表合并。

默认单项下载/图片 32 MiB、每轮当前及引用附件合计 4 个和 64 MiB、下载超时 60 秒，inline 10 MiB、WebSocket 16 MiB；超出 inline 预算时自动尝试上限 256 MiB 的分块上传。失败下载仍占本轮为其保留的完整字节额度，成功时按实际大小计费。引用默认关闭，近期补入另需开启。原 YAML 的显式限制继续生效。

读取工具现在属于 `napcat_qq_read`，发送属于 `napcat_qq`。旧 `qq_tools.enabled: true` 仍允许两类调用，但只配置 `napcat_qq` 的会话升级后需加入 `napcat_qq_read` 才能发现读取工具。只读部署使用 `qq_tools.read_enabled: true`，并保持 `enabled: false`。详见 [QQ 工具](QQ_TOOLS.md)。

## 认证流式下载与兼容模式

`media.download_mode` 默认 `auto`：图片调用 `download_file_image_stream`，语音调用 `download_file_record_stream` 并请求 MP3，视频/文件调用 `download_file_stream`。这些 action 复用当前已核验账号的 WebSocket，不转发 token 到媒体 URL。

| 模式 | 行为 |
| --- | --- |
| `auto` | 优先安全不透明文件 ID 的流式下载；明确不支持 action 或没有安全 ID 时使用受控 HTTP |
| `stream` | 必须有安全 ID 和流式能力；不转 HTTP |
| `http` | 显式采用入站 HTTP 策略；失效 URL 可从原消息刷新一次 |

明确不支持 action 指 NapCat 的 `status=failed`、整数 `retcode=1404` 和 `stream=normal-action`，且尚未收到流数据。文件标识明确不可用时，普通消息最多用一次已核验 `get_msg` 更新同一附件；更新后仍明确不可用，才允许 `auto` 使用安全 HTTP locator。`auto` 安全回退后的 HTTP 与显式 `http` 都可对 403/404/410 失效响应执行同一份最多一次原消息刷新额度。引用 TTL 和来源不延长。合并转发节点或仅来自通知的引用不猜测刷新来源，应重新查询已核验转发/文件历史。

错误次序、重复/缺失块、非规范 base64、超额字节、超时、断线、账号/连接变化和权限失败均中止，不通过 HTTP 绕过。每个 echo 有帧数和缓冲字节上限；实际字节数、块号、块大小和完成总数须一致。语音初始大小可能是转换前大小，以实际 MP3 字节和完成总数为准。只有非空、校验完成的流会原子发布缓存文件。远端文件名只可贡献白名单文档扩展名，远端本地路径不会在 Hermes 上打开。

## 分离入站与出站 HTTP 策略

最小修改放在 `gateway.platforms.napcat.extra` 下：

```yaml
media:
  outbound:
    mode: public
```

这样出站可下载任意公网 HTTP(S) 图片，入站继续继承原有策略。`public` 不跳过实际 DNS 结果检查、IP 字面量检查、逐跳重定向校验、TLS 证书验证、大小和超时限制，也不使用代理环境变量。两方向的 HTTP session 和 private-origin 信任范围独立。

`allowlist` 支持精确域名和 `*.example.com` 形式的子域名通配符。通配符匹配 `cdn.example.com`、`a.b.example.com`，不匹配根域名 `example.com`、`badexample.com` 或 `example.com.evil.test`；需要根域名时单独添加。匹配不区分大小写，忽略末尾的点。仅支持开头的 `*.`，其后必须是至少两段的 ASCII DNS 域名（国际化域名使用 Punycode）；不接受单独的 `*`、中间通配符或 IP 通配符。

例如，为入站图片允许 QQ CDN 子域名，可在 `gateway.platforms.napcat.extra` 下配置：

```yaml
media:
  inbound:
    mode: allowlist
    allowed_hosts:
      - multimedia.nt.qq.com
      - '*.qpic.cn'
      - grouptalk.c2c.qq.com
```

YAML 中的通配符必须加引号。规则也适用于 `media.outbound.allowed_hosts` 和旧的 `media.allowed_hosts`。自定义列表替换该方向的默认列表；上例保留了默认四个 QQ 主机的访问，并允许其他 `qpic.cn` 子域名。默认配置仍使用精确主机，升级不会自动扩大访问范围。只为信任的域名后缀配置通配符，它会授权其所有子域名。

空列表拒绝全部普通主机。通配符不授予私网访问权限，实际连接时仍检查 DNS 结果，每次重定向也重新校验。`public` 忽略普通域名列表，但不允许访问非公网目标。需要容器内图床时，只为相应方向明确指定协议、主机与端口：

```yaml
media:
  outbound:
    mode: public
    trusted_private_origins: ['http://media-files:8080']
```

不得把管理 API、云元数据地址或有敏感 GET 接口的内网服务加入信任列表。没有“放行全部内网”开关。

## 大小、缓存与发送方式

以下示例使用默认限制；公网 URL 仍是显式授权：

```yaml
ws_max_bytes: 16777216       # 16 MiB，NapCat 和中间代理也必须允许对应消息大小
media:
  download_mode: auto
  inbound:
    mode: allowlist
  outbound:
    mode: public
  max_bytes: 33554432       # 32 MiB，下载和本地图片检查的单项上限
  max_attachments: 4
  max_turn_bytes: 67108864
  inline_max_bytes: 10485760
  outbound_roots: ['/srv/qq-output']
```

base64 长度按 `4 * ceil(n / 3)` 计算，另计 UTF-8 JSON、消息段和 echo 开销。运行时 inline 预算预留 16 KiB 开销，实际发送前再次按整条请求检查；保留旧的小 WS 上限时自动缩小有效 inline 预算。增大插件上限不能证明 NapCat、代理和 QQ 实际接受该尺寸，部署时必须测试。

普通 `qq_send_message` 的多图/图文消息超预算时，按消息段边界保序拆分，引用段只保留在第一条。发送前先规划整批；任何单段仍然超限，会在首条发送前拒绝。发送中途失败返回已知 `message_ids`、`partial` 及 `delivery_uncertain`，不会自动重试整个批次。合并转发卡片保持单次动作，不拆卡；超限应采用共享文件或减少节点。

同机或共享卷部署可避免 base64 膨胀：

```yaml
media:
  outbound:
    mode: public
  outbound_roots: ['/srv/qq-output']
  shared_paths:
    - hermes: /srv/qq-shared
      napcat: /data/qq-shared
  shared_cache_dir: /srv/qq-shared/staging
```

本地文件、URL 下载和受控媒体引用使用相同的传输选择：已在映射目录内则直接给出文件 URI；否则有 `shared_cache_dir` 就复制到专用暂存目录；没有共享暂存目录则使用受限 base64，超限时按配置选择 NapCat 分块上传。本地图片和流式入站图片检查常见文件头；流式语音检查 MP3、视频检查 MP4/WebM/AVI 标识。这不等于完整解码或解析沙箱。

`shared_cache_dir` 必须在显式共享映射内。Hermes 写入、NapCat 只读，两端必须实际挂载同一份存储；路径映射不提供跨主机同步。新文件权限为 0600，部署者须通过一致的运行 UID 或受控 ACL 使 NapCat 可读，不能靠把整个缓存设为公开来解决权限。

下载缓存与共享暂存共同计入 `cache_max_bytes`；复制可能同时占用两份空间。并发下载保留额度，额度不足会拒绝新文件。仅按 UUID 命名且过期的插件普通文件会在后续下载/暂存时被清理，其他文件不删除。清理不是定时删除服务；TTL 到达不代表磁盘立即擦除。不要让不可信本地用户修改这些目录。

接收缓存使用当前 Hermes profile 的 `get_hermes_dir('cache/documents', 'document_cache') / 'napcat'`。Hermes 会解析新缓存目录或已存在的旧 `document_cache` 目录，插件只管理其中自己的 UUID 文件。文件工具获得 `to_agent_visible_cache_path` 转换后的路径；宿主下载路径与 sandbox 可见路径可能不同。部署者仍须验证目标 sandbox 实际能读取新下载的字节。

## 当前、引用、近期与历史附件

```yaml
media:
  references:
    enabled: true
    ttl_seconds: 1800
    max_entries: 1024
    attach_quoted: true
    attach_recent: true
    recent_seconds: 120
    recent_limit: 1
```

引用涵盖 `image`、`record`、`video`、`file`，保存账号、会话、消息、发送者、统一附件索引、类型和受限 locator，向上下文/工具只暴露随机 `media_id` 及有界显示元数据。图片保持 `qqimg_...` 前缀，其他类型使用 `qqmedia_...`；调用时采用实际返回 ID，不推测标识。有效期从原消息时间计算，再观察可补充缺失 locator，但不改变来源或延长期限。重启、淘汰、过期后失效；历史附件恢复需要有效时间及可用文件 ID/安全 URL。

当前附件先占本轮共享数量/字节预算，随后 `attach_quoted` 可补入同会话明确引用的附件，即使当前消息也有附件。只有没有当前附件且没有明确引用时，`attach_recent` 才补入当前发言人自己的近期附件，最多 `recent_limit` 项。不需要自动近期补入时只启用引用。各类附件都进入正常 `MessageEvent.media_urls/media_types`，Hermes 决定实际处理能力。

群里“先发附件，再 @ 询问”需要同时启用 `group_context.enabled` 和 `observe_untriggered`，且作者在观察范围内。普通观察只记录元数据，不持续下载全群附件；历史查询保持 `disable_get_url=true`。`observe_all_members` 仍需管理员显式授权。主动参与轮次不自动下载附件。

私聊 `offline_file` 和群 `group_upload` 通知在账号、目标、作者、时间、文件 ID/URL、名字和大小校验后进入正常附件处理。相同会话/作者/文件标识在通知与普通消息之间有界去重；URL-only 通知无法证明其与另一个 opaque ID 相同，不能保证跨形式去重。通知派生 ID 不能用于 `get_msg` 或历史分页锚点。

获准私聊文件通知按私聊消息接收；群文件通知没有 @/前缀，不单独触发 Agent。启用群背景时可按观察策略保留其引用，之后由明确请求或受控引用读取。

`qq_get_message`、私聊/群近期消息和已核验父消息的 `qq_get_forward` 可返回有效媒体引用，不自动下载附件。自动引用/近期补入不依赖 QQ 工具开关；模型主动取媒体需要只读授权，原样发送另需发送授权：

```json
{"media_id": "qqmedia_实际返回的标识"}
```

这是 `qq_get_media` 的参数。结果含受控、Agent 可见的缓存路径、MIME、大小和可发送的 `source`；路径 JSON 本身不代表内容已被理解。需要直接理解时使用 `qq_read_media`，见下一节。

`qq_send_media` 可以原样发回：

```json
{"media_type": "image", "source": "media:qqimg_实际返回的标识"}
```

`qq_send_message` 的图片来源同样接受 `media:<id>`；`qq_send_media` 可原样发送其他种类。引用只能在同账号、同会话使用，即使管理员获得一般跨会话发送权限也不能把引用带到另一聊天。缓存不会加入 `outbound_roots`。读取和发送复核请求者及原作者可见性。

收到所属账号的 `friend_recall` / `group_recall` 后引用失效，在下载结束、传入本轮事件前及发送写锁内复查。排队期间撤回/过期的附件不会发送。已提交给模型、已返回给本地工具、已发出的 QQ 消息无法收回；丢失的撤回通知也无法可靠重建。引用权限不是任意本地工具的 OS 沙箱。

## Hermes 内容理解

`qq_read_media` 需要当前会话的只读工具授权及有效引用：

```json
{"media_id":"qqmedia_实际返回的标识","question":"请解释附件中的主要信息"}
```

| 类型 | Hermes 接口与结果 |
| --- | --- |
| 图片 | 注册的 `vision_analyze` handler，按 Hermes 配置选择原生多模态或辅助视觉；原生 `_multimodal` 返回保持原样 |
| 语音 | Hermes `transcribe_audio(..., source='gateway')`；尊重 runner 的 STT 开关，失败时仅尝试已安装的本地后端，结果注明 fallback |
| 视频 | 注册的 `video_analyze` handler，需要其真实依赖/提供商可用 |
| 文档 | 注册的 `read_file`，默认从第 1 行读取 200 行，可选 `offset`/`limit`，单页上限 500 行；普通字符串结果截断到 16,000 字符 |

这些调用在请求者的 Hermes profile/任务上下文执行。没有安装或启用的能力返回明确错误，不假装理解内容。文档截断会标记 `truncated`；截断时不返回可能跳过内容的 `next_offset`。分页限制的是结果量，Hermes 文档提取仍可能解析整个文件，源码基准的文档字节上限是 50 MiB。图片文件头验证也不能取代媒体解码隔离。

## 独立定时媒体投递

正向 standalone sender 接受 Hermes 的 `media_files=[(local_path, is_voice), ...]`，在首条 QQ 消息前检查全部目标、路径、大小和发送引用。`is_voice=true` 选择语音；否则图片/视频按文件类型选择，普通音频作为文件。`force_document=true` 覆盖分类，全部按文档上传。使用与 Gateway 相同的共享/inline/stream 准备路径；不接受无会话身份的入站 `media:<id>`。反向独立投递明确拒绝。

文字与媒体可分成多次 action；中途失败保留已知消息 ID 和 `partial`/`delivery_uncertain`，不能重发整个组合。已有 QQ 媒体要原样转给同会话，可使用 `qq_send_forward` 的 `message_id` 节点让 NapCat 服务端转发，省去下载和上传。

## 验收边界

新增测试覆盖配置迁移、两方向私网信任隔离、重定向拦截、共享暂存/额度、URL 刷新、引用期限与归属、懒加载、撤回竞态、队列中失效和多图部分成功。HTTP 使用本地测试服务；Hermes、QQ action 和 DNS 返回值按场景使用替身。没有执行真实 QQ、真实 Gateway/视觉模型联调，也未验证真实 Windows QQ；NapCat 分块接口已做源码核验与正反向 loopback 协议测试。

部署验收应覆盖私聊/群聊四种媒体、文件通知、当前加引用预算、先发附件后问、历史/合并转发、下载模式与错误回退、共享卷和 sandbox 路径、视觉/STT/视频/文档能力及独立 cron 组合投递。先用测试账号检查实际 QQ 内容，记录版本、WS 限制和模型处理结果。

分块上传、`base64://` 与 data URI 直发的完整配置、内存边界和失败处理见 [STREAM_UPLOAD](STREAM_UPLOAD.md)。
