# Hermes NapCat Plugin

把 QQ 私聊和群聊接入 Hermes Agent。插件通过 NapCat 的 OneBot v11 WebSocket 收发消息，由 Hermes Gateway 管理 Agent 调用和会话。

```text
QQ ↔ NapCat ↔ OneBot v11 WebSocket ↔ Hermes NapCat Plugin ↔ Hermes Gateway ↔ Agent
```

支持文本、图片、语音、视频、文件和合并转发，并可按需启用群聊背景、主动参与及 QQ 工具。插件以独立 Python 包和 Hermes 目录插件安装，不修改 Hermes 核心代码，也不依赖 NapCat HTTP API。

## 功能概览

| 能力 | 支持范围 |
| --- | --- |
| 私聊与群聊 | 用户白名单；群聊同时检查群白名单，支持 @机器人、回复机器人和 `/ai` 前缀触发 |
| 文本收发 | 引用、长文本分段、结构化消息段；模型输出的 CQ 字符串按普通文本发送 |
| 图片与其他媒体 | 受控 URL 下载、本地文件、base64/data URI、共享目录及 NapCat 分块上传；不提供音视频转码 |
| 图片引用 | 可选同会话引用图、同一发言人的近期图片补入，以及通过工具读取或原样回图 |
| 群聊背景 | 可选实时观察、有界历史回填，保留发言人、时间、@和引用信息；会话仍由 Hermes 管理 |
| 主动参与 | 可选规则或无工具分类器判断；默认关闭，启用后默认 dry-run，受冷却、预算和回复时效限制 |
| Agent QQ 工具 | 可选 `napcat_qq` 工具集，支持图文/媒体发送、合并转发、消息/会话读取和近期群消息查询 |
| 连接与可靠性 | 正向/反向 WebSocket、token 与登录账号核验、心跳、重连、并发请求关联、有界队列、限流和内存去重 |
| 定时投递 | Hermes 原生目标解析与 cron sender；独立进程仅支持正向连接的文本投递 |
| 运维 | 配置检查、连接探测、人工测试发送、安装与 Hermes 接口检查脚本 |

群聊背景、主动参与、图片引用和 Agent QQ 工具均需显式启用。群工具集默认为空；普通观察不下载附件，也不授予被观察者调用 Agent 的权限。

当前不包含群管理、QQ 空间、持久群历史、Relay、持久消息队列或跨重连上传续传。发送结果不确定时不会自动重发；内存去重也不提供跨进程重启的投递保证。

## 快速接入

以下步骤使用同机正向连接：插件连接 NapCat 的 WebSocket Server。需要已经能运行的 Hermes、已登录 QQ 的 NapCat，以及 Python 3.12+ 和 `uv`。本仓库不安装 Hermes、NapCat 或 QQ 客户端。

所有仓库命令均在 `hermes-napcat` 项目目录执行。示例中的 QQ 号、密钥和安装路径需要替换为实际值。

### 1. 安装项目依赖

```sh
uv sync --group dev
```

依赖由 [pyproject.toml](pyproject.toml) 声明，仓库提供 `uv.lock`。

### 2. 配置 NapCat OneBot

在 NapCat 的 OneBot 网络配置中启用 **WebSocket Server**：

| 设置 | 值 |
| --- | --- |
| `host` | `127.0.0.1` |
| `port` | `3001` |
| `messagePostFormat` | `array` |
| `reportSelfMessage` | `false` |
| `enableForcePushEvent` | `true` |
| `token` | 自行生成的随机密钥 |

完整片段见 [正向连接示例](examples/napcat-onebot-forward.json)，合并所需网络配置即可。这里的 token 是 **OneBot 访问密钥**，与 NapCat WebUI 登录 token 不同。

可以用以下命令生成密钥：

```sh
uv run --no-project python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. 配置 Hermes profile 环境变量

将以下内容写入运行 Gateway 的 Hermes profile `.env`，其中 `NAPCAT_TOKEN` 与 NapCat 中填写的值相同：

```dotenv
NAPCAT_TOKEN=替换为生成的随机密钥
NAPCAT_SELF_ID=机器人登录的QQ号
NAPCAT_ALLOWED_USERS=你的QQ号,另一个获准QQ号
NAPCAT_ALLOW_ALL_USERS=false
NAPCAT_HOME_CHANNEL=private:你的QQ号
```

`NAPCAT_ALLOWED_USERS` 同时供插件和 Hermes Gateway 鉴权读取，部署时必须设置；只在插件 YAML 中写 `allowed_users` 可能仍被 Gateway 拒绝。空白名单默认拒绝用户。`NAPCAT_HOME_CHANNEL` 是可选的默认投递目标。

环境变量参考 [examples/.env.example](examples/.env.example)。密钥保存在操作者的 profile 环境中，不要提交到仓库。多个 QQ 账号使用独立的 Hermes profile、配置和缓存目录。

### 4. 安装插件到 Hermes

指定 **实际运行 Hermes 的 Python 解释器** 和 **Gateway 使用的 profile 目录**：

```sh
uv run --no-project python scripts/install_plugin.py \
  --hermes-python /opt/hermes-agent/.venv/bin/python \
  --hermes-home /srv/hermes-home
```

Windows 的解释器路径通常以 `Scripts/python.exe` 结尾。`--hermes-home` 必须与 Gateway 使用的 `HERMES_HOME` 或 profile 目录一致。

安装脚本会依次安装 Python 包、使用该解释器检查真实 Hermes 接口，然后将目录入口安装到 `<hermes-home>/plugins/napcat/`。它不会改写现有 Hermes 配置。升级时追加 `--upgrade`，旧入口会备份到 `<hermes-home>/plugin-backups/`。升级需要同时更新 Python 包和目录入口，仅替换 wheel 不足以更新工具发现信息。

### 5. 启用平台

将下面的键合并到该 profile 的 `config.yaml`，保留已有模型、提供商和其他平台配置：

```yaml
group_sessions_per_user: true
plugins:
  entries:
    napcat:
      enabled: true
gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        mode: forward
        ws_url: ws://127.0.0.1:3001
        allowed_groups: ['获准群号']
        admins: ['你的QQ号']
        group_mention: true
        group_reply_to_bot: true
        group_prefixes: ['/ai']
        group_toolsets: []
platform_toolsets:
  napcat: []
```

`admins` 中的用户必须同时出现在 `NAPCAT_ALLOWED_USERS`。只用私聊时可将 `allowed_groups` 设为 `[]`。完整示例见 [Hermes 配置](examples/hermes.config.yaml)。

先保持工具集为空，确认基础收发。私聊工具由 `platform_toolsets.napcat` 控制，群聊用 `group_toolsets` 单独限制。`group_sessions_per_user: true` 按群成员划分会话；改为共享会话前应确认群成员接受共享上下文。

### 6. 启动并验证

沿用现有 Hermes 服务启动方式，或在专用工作目录运行：

```sh
hermes gateway run
```

从白名单 QQ 私聊机器人，再在白名单群中尝试 `@机器人 你好`、`/ai 你好`，以及回复机器人消息。群触发还会校验 @/回复归属和前缀边界，机器人自己的消息不会触发 Agent。随后验证未授权用户和群无法调用。

需要单独诊断连接时，先修改 [examples/napcat.yaml](examples/napcat.yaml) 中的账号、群和管理员配置，并在当前终端进程环境中设置 `NAPCAT_TOKEN`、`NAPCAT_SELF_ID`、`NAPCAT_ALLOWED_USERS` 等变量。**诊断 CLI 不会自动读取 Hermes 的 `.env`。** 它接受对应 `gateway.platforms.napcat.extra` 的扁平 YAML，不接受完整 Hermes 配置。

```sh
uv run hermes-napcat --config examples/napcat.yaml check
uv run hermes-napcat --config examples/napcat.yaml probe
uv run hermes-napcat --config examples/napcat.yaml send \
  --target private:你的QQ号 --message 'NapCat transport test'
```

- `check`：只校验配置，不访问网络。
- `probe`：连接 OneBot，核验登录账号并读取状态；不证明 Gateway 已加载插件。
- `send`：向指定白名单目标发送一条真实 QQ 消息。

目标格式为 `private:QQ号` 或 `group:群号`，不接受裸数字。Hermes cron 投递平台填写 `napcat`，目标和 `NAPCAT_HOME_CHANNEL` 使用同一格式。

## 按需启用

### 群聊背景与主动参与

合并 [群聊配置示例](examples/group-chat.config.yaml)，用 `group_context.enabled: true` 开启背景观察和按需历史回填。默认观察范围受用户白名单限制；`observe_all_members: true` 需要管理员明确授权，并让群成员知晓数据用途。

`proactive_assist` 默认关闭。启用后先保留 `dry_run: true`，只判断是否应参与；实际发言需要 `dry_run: false` 且 `group_toolsets: []`。历史回填有数量和时间边界，不保证补齐断线期间全部消息。配置、分类器和验收步骤见 [群聊指南](docs/GROUP_CHAT.md)。

### Agent QQ 工具

在需要的会话工具集中加入 `napcat_qq`，同时设置 `qq_tools.enabled: true`。工具默认绑定当前 QQ 会话。跨会话调用还要求 `allow_cross_chat: true`、当前用户属于 `admins`，并且目标通过白名单检查。

工具可以发送图文、媒体和合并转发，读取消息、会话及近期群消息。`qq_get_media` 返回受控图片缓存路径，需要会话已有的视觉读取工具消费；工具返回的 JSON 不会自动成为模型视觉输入。参数和配置见 [QQ 工具指南](docs/QQ_TOOLS.md) 与 [配置示例](examples/qq-tools.config.yaml)。

### 媒体与图片引用

入站和出站下载策略独立，默认使用 QQ 域名白名单。需要发送公网图床内容时，在 `gateway.platforms.napcat.extra` 下设置：

```yaml
media:
  outbound:
    mode: public
```

`public` 仍检查 DNS、重定向、非公网地址、TLS、大小和超时。私有图床需要精确配置 `trusted_private_origins`；本地发送目录由 `outbound_roots` 授权。完整策略及共享卷配置见 [媒体指南](docs/MEDIA.md) 和 [配置示例](examples/media.config.yaml)。

| 限制 | 默认值 |
| --- | --- |
| 单项下载 / 本地图片 | 32 MiB |
| inline 原始媒体 | 10 MiB，另受实际 WebSocket 预算限制 |
| 单条 WebSocket 消息 | 16 MiB，含 base64 和 JSON 开销 |
| 分块上传 | 256 MiB，仍受具体媒体来源的大小限制 |
| 每轮入站附件 | 4 个 |

已有配置中的显式限制继续生效；例如诊断示例保留了 10 MiB 的下载上限。传输优先使用共享路径或专用共享暂存，再选择 base64；超过 inline 预算时默认尝试 NapCat 分块上传。共享路径要求两端实际挂载同一份存储，并让 NapCat 有受控读取权限。NapCat、代理和 QQ 的实际限制还需单独验证。分块参数、base64 格式与失败处理见 [上传指南](docs/STREAM_UPLOAD.md)。

设置 `media.references.enabled: true` 可启用同会话引用图补入；`attach_recent` 另行控制同一发言人的近期图片。群里“先发图再 @”还需要群上下文观察。普通观察只记录短期引用，不下载全群附件；主动参与不会自动读图。引用过期或收到撤回通知后停止后续读取/发送，已交给模型的数据无法收回，断线期间丢失的撤回通知也无法可靠补齐。

配置修改后需重启 Gateway。

## 反向连接与容器部署

反向模式由 NapCat WebSocket Client 连接插件。在 `gateway.platforms.napcat.extra` 中配置：

```yaml
mode: reverse
listen_host: 127.0.0.1
listen_port: 3002
ws_path: /onebot/v11
```

NapCat 连接地址设为 `ws://127.0.0.1:3002/onebot/v11`，使用同一个 OneBot token，参考 [反向连接示例](examples/napcat-onebot-reverse.json)。插件接受一条 Universal 双向连接，并校验 Bearer token、账号与客户端角色。监听启动后，需要 NapCat 实际连入才能发送。

两个容器不共享 loopback 地址。跨容器使用专用网络中的服务名并调整监听地址；非 loopback 明文 WebSocket 需要显式设置 `allow_insecure_ws: true`。跨主机使用受控隧道或校验证书的 WSS，反向监听的 TLS 由反向代理终止。代理应保留 `Authorization`、`X-Self-ID`、`X-Client-Role`，避免记录敏感请求头。不要公开 OneBot 端口。

反向模式不支持独立 cron sender，应通过运行中的 Gateway 投递。诊断程序与 Gateway 不能同时占用同一个反向监听端口。

## 权限与故障排查

网关控制命令仅限 `admins`，仍受 Hermes 自身权限约束；平台 `/update` 已禁用。发送目标同样检查白名单。插件不会向模型开放任意 OneBot action，也不会将入站消息或 `get_image` 返回的本地路径当作 Hermes 文件读取。完整边界见 [SECURITY.md](SECURITY.md)。

| 现象 | 检查项 |
| --- | --- |
| `probe` 成功，但 Agent 不回复 | 插件是否加载、Hermes Python 与 profile 是否正确、`NAPCAT_ALLOWED_USERS` 是否对 Gateway 可见 |
| 私聊正常，群聊不触发 | 用户与群白名单、@目标、回复来源以及 `/ai` 的前缀边界 |
| 升级后找不到 QQ 工具 | 用安装脚本加 `--upgrade` 同时更新包与目录入口，确认工具开关和会话工具集，重启 Gateway |
| 图片或文件被拒绝 | 下载策略、大小限制、允许的本地目录、共享挂载权限及 NapCat 分块接口支持情况 |
| 返回 `partial` 或 `delivery_uncertain` | 先检查 QQ 中已收到的内容及返回的消息 ID，避免重试导致重复发送 |
| Hermes 升级后接入失败 | 在真实 Hermes 环境重跑接口检查，再按测试文档执行收发和工具验收 |

可独立运行接口检查；它会导入 Hermes 并核对接口，不启动模型或连接 QQ：

```sh
/opt/hermes-agent/.venv/bin/python scripts/check_hermes_contract.py
```

## 开发与文档

项目使用 Python 3.12+、`src/` 布局和 `uv`。协议、传输模块独立于 Hermes，Hermes 依赖位于 Gateway 接入边界。

```sh
uv run pytest -q
uv run ruff check .
uv build
```

| 文档 | 内容 |
| --- | --- |
| [架构](docs/ARCHITECTURE.md) | 模块职责与消息链路 |
| [群聊](docs/GROUP_CHAT.md) | 背景观察、历史回填、主动参与和隐私边界 |
| [媒体](docs/MEDIA.md) | 下载策略、共享目录、图片引用和撤回 |
| [流式上传](docs/STREAM_UPLOAD.md) | 分块上传、base64 输入、资源限制与失败语义 |
| [QQ 工具](docs/QQ_TOOLS.md) | 工具参数、会话权限和部分成功处理 |
| [兼容性](docs/COMPATIBILITY.md) | 上游源码核验基准与升级约束 |
| [测试与验收](docs/TESTING.md) | 模拟测试记录和真实部署验收步骤 |
| [开发计划](docs/ROADMAP.md) | 后续工作范围 |

许可证：[MIT](LICENSE)。
