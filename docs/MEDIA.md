# 图片收发、下载策略与受控引用

本次改动基于 `main` 的 `741820f`。HTTP 与 HTTPS 原本就可用；新功能解决精确域名策略共用、下载后不能使用共享目录、整条消息超限及历史图片只有占位的问题。它不修改 Hermes 核心，也不将任意网络 URL 直接交给 NapCat 下载。

## 升级和默认行为

升级 Python 包及目录插件入口后重启 Gateway。配置没有热加载。旧配置继续有效：未配置 `media.inbound` 或 `media.outbound` 的方向，继承旧的 `media.allowed_hosts`、`media.trusted_private_origins`，默认仍为 allowlist。显式配置某个方向后，该方向使用自己的完整策略和默认值，不与旧列表合并。

默认下载/图片 32 MiB、4 个入站附件、下载超时 60 秒，inline 10 MiB、WebSocket 16 MiB；超出 inline 预算时自动尝试上限 256 MiB 的 NapCat 分块上传。原 YAML 的显式限制继续生效。引用功能默认关闭，近期图片自动补入还需要单独开启。升级不会自行放开网络访问、扩大群观察范围或增加模型工具权限。

## 分离收图与发图策略

最小修改放在 `gateway.platforms.napcat.extra` 下：

```yaml
media:
  outbound:
    mode: public
```

这样出站可下载任意公网 HTTP(S) 图片，入站继续继承原有策略。`public` 不跳过实际 DNS 结果检查、IP 字面量检查、逐跳重定向校验、TLS 证书验证、大小和超时限制，也不使用代理环境变量。两方向的 HTTP session 和 private-origin 信任范围独立。

`allowlist` 仍只支持精确域名；空列表拒绝全部普通主机，通配符会导致配置错误。自定义列表会替换默认四个 QQ 主机。`public` 忽略普通域名列表，但不允许访问非公网目标。需要容器内图床时，只为相应方向明确指定协议、主机与端口：

```yaml
media:
  outbound:
    mode: public
    trusted_private_origins: ['http://media-files:8080']
```

不得把管理 API、云元数据地址或有敏感 GET 接口的内网服务加入信任列表。没有“放行全部内网”开关。

## 图片大小与传输方式

以下示例使用本次大小默认值；公网 URL 仍是显式授权：

```yaml
ws_max_bytes: 16777216       # 16 MiB，NapCat 和中间代理也必须允许对应消息大小
media:
  inbound:
    mode: allowlist
  outbound:
    mode: public
  max_bytes: 33554432       # 32 MiB，下载和本地图片检查的单项上限
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

本地文件、下载后的 URL 图片、已接收的受控图片使用相同的传输选择：已在映射目录内则直接给出文件 URI；否则有 `shared_cache_dir` 就复制到专用暂存目录；没有共享暂存目录则使用受限 base64，超限时按配置选择 NapCat 分块上传。下载后的图片不再强制 base64。本地图片会检查常见格式文件头和单图大小；这不等于完整解码验证。

`shared_cache_dir` 必须在显式共享映射内。Hermes 写入、NapCat 只读，两端必须实际挂载同一份存储；路径映射不提供跨主机同步。新文件权限为 0600，部署者须通过一致的运行 UID 或受控 ACL 使 NapCat 可读，不能靠把整个缓存设为公开来解决权限。

下载缓存与共享暂存共同计入 `cache_max_bytes`；复制可能同时占用两份空间。并发下载保留额度，额度不足会拒绝新文件。仅按 UUID 命名且过期的插件普通文件会在后续下载/暂存时被清理，其他文件不删除。清理不是定时删除服务；TTL 到达不代表磁盘立即擦除。不要让不可信本地用户修改这些目录。

## 引用旧图、近期图片与原样发回

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

引用保存账号、会话、消息 ID、发送者、图片索引、受限文件标识和必要下载信息，向上下文/工具只暴露随机 `qqimg_...` 标识，不输出原始 URL 或 NapCat 文件路径。引用有效期从原消息时间计算，重复读取不会延长；重启后失效，容量满时淘汰。历史消息必须有有效时间和可用 URL 或文件 ID；任意久远的图片不保证可以恢复。

当前消息含附件时优先处理当前附件，不自动追加历史图片。没有当前附件时，明确引用优先；`attach_quoted` 可将同会话图片实际下载并加入本轮 `MessageEvent.media_urls`。否则启用 `attach_recent` 后，才补入当前发言人自己在最近时间窗内的图片，最多 `recent_limit` 张。近期策略不猜测语义：启用后，无附件消息即可能补入该时间窗内的图片；不需要这种行为时只启用引用。

群里“先发图，再 @ 询问”需要同时启用 `group_context.enabled` 和 `observe_untriggered`，且发图用户在允许的观察范围内。普通观察只记录引用元数据，不持续下载全群图片；历史查询继续使用 `disable_get_url=true`。`observe_all_members` 依然是单独的管理员授权。主动参与轮次不自动下载图片。

当前图片 URL 下载失败时，只用受限的原始文件 ID 调用一次 `get_image` 刷新 URL，再经过同一个入站安全下载器。刷新再次失败会报告未读取，不无限重试。来自消息或 `get_image` 的本地路径、文件 URI、base64 不会被当作 Hermes 本地文件或备用输入；NapCat 与 Hermes 可能位于不同机器。

自动引用/近期补图不依赖 QQ 工具开关。模型主动取图或原样发回则需要原有 `qq_tools.enabled` 与 Hermes 工具集授权：

```json
{"media_id": "qqimg_实际返回的标识"}
```

这是 `qq_get_media` 的参数。结果含本机受控缓存路径、MIME、大小和可发送的 `source`。工具 JSON **不会自动变成模型的视觉输入**，需要当前会话已有的视觉读取工具消费该路径。自动补图进入的是正常 Hermes 媒体事件，但 Hermes 和所选模型仍必须实际支持视觉。

`qq_send_media` 可以原样发回：

```json
{"media_type": "image", "source": "media:qqimg_实际返回的标识"}
```

`qq_send_message` 的图片来源同样接受 `media:<id>`。图片引用只能在同账号、同会话使用；即使管理员开启一般跨会话发送，也不能把这种引用带到另一个聊天。不会为了回图开放整个入站缓存目录。`qq_get_message` 和群历史输出可提供有效图片标识，但不授予新权限；下载时还要复核请求者及原发送者的可见性。

收到所属账号的 `friend_recall` / `group_recall` 后引用失效，并在下载结束、传入本轮事件前和等待发送锁后复查。已发送给模型、已返回给有本地读取能力的工具、已发出的 QQ 消息无法收回；断线期间未收到的撤回也无法可靠判断。引用权限不是任意本地文件工具的 OS 沙箱。群工具默认仍为空，实际主动参与仍要求无工具群会话。

## 验收边界

新增测试覆盖配置迁移、两方向私网信任隔离、重定向拦截、共享暂存/额度、URL 刷新、引用期限与归属、懒加载、撤回竞态、队列中失效和多图部分成功。HTTP 使用本地测试服务；Hermes、QQ action 和 DNS 返回值按场景使用替身。没有执行真实 QQ、真实 Gateway/视觉模型联调，也未验证真实 Windows QQ；NapCat 分块接口已做源码核验与正反向 loopback 协议测试。

部署验收应覆盖私聊和群聊：新图床 HTTP/HTTPS 图片、1–32 MiB 截图、共享卷权限、引用和先图后问、多图拆分、跨会话拒绝、撤回与过期，以及发送后断线。先用测试账号检查 QQ 实际收到的内容，记录两端版本、WS 限制和视觉处理结果。独立 cron sender 仍仅支持文本。

分块上传、`base64://` 与 data URI 直发的完整配置、内存边界和失败处理见 [STREAM_UPLOAD](STREAM_UPLOAD.md)。
