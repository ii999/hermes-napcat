# Hermes NapCat Plugin

通过 OneBot v11 双向 WebSocket，把 NapCat QQ 接入 Hermes Gateway。项目使用 Python 3.12+、`uv` 和 `/src` 布局，以独立 Python 包与目录插件入口分发，不修改 Hermes 源码。

当前版本：**0.1.0，待真实 QQ 联调的首版实现**。我们核验了 Hermes `v2026.9.14`（0.21.3）的源码接口，并运行本地协议、网络与安全边界测试。完整 Hermes Gateway、模型调用、QQ 登录和媒体编码仍需要在部署环境验收。详见 [测试记录](docs/TESTING.md)。

```text
QQ ↔ NapCat ↔ OneBot v11 WebSocket ↔ napcat 插件 ↔ Hermes Gateway ↔ Agent
```

默认由插件连接 NapCat WebSocket Server。反向模式由 NapCat 连接插件的 `/onebot/v11`。消息事件、API 请求及 `echo` 响应共用一条连接，插件不依赖 NapCat HTTP API。

## 首版范围

| 能力 | 实现与边界 |
| --- | --- |
| 私聊、群聊 | 用户白名单；群聊还必须满足群白名单；自己发送的消息不触发 Agent |
| 群触发 | `@机器人`、经过服务端校验的回复、带词边界的 `/ai` 前缀 |
| Hermes 接入 | 原生 `BasePlatformAdapter`，`SessionSource`、账号上下文、媒体事件；会话由 Gateway 管理 |
| 文本回复 | 结构化消息段、引用、分段发送；不会把模型输出的 CQ 字符串解释为控制指令 |
| WebSocket | 正向/反向、Bearer token、登录账号核验、心跳、重连、超时、并发 echo 关联 |
| 消息可靠性 | 有界队列、同聊天顺序、跨聊天并行、限流、内存去重；发送结果不确定时不会重发 |
| 入站媒体 | 图片、语音、视频、文件的 URL 下载与事件映射；缺 URL 或格式不支持时给出未读取说明 |
| 出站媒体 | 本地图片/音频/视频通过小文件 base64 或共享路径发送；文档上传要求共享路径 |
| 权限 | 网关控制指令限 `admins`；群聊默认无模型工具；发送目标也检查白名单 |
| 主动推送 | 注册原生目标解析与 cron standalone sender；独立进程仅支持正向连接的文本推送 |
| 运维 | 配置检查、连接探测、人工测试发送、安装脚本、私有 GitHub 仓库发布脚本 |

本版不包含 QQ 群管理/空间工具集、被动群历史收集、Relay、持久消息队列、跨机器大文件流式上传或语音转码。音频能否进入 Hermes STT、音视频能否在 QQ 播放，还取决于实际格式与运行环境。开发计划见 [ROADMAP](docs/ROADMAP.md)。

## 1. 准备环境

使用已安装且能启动的 Hermes，以及已登录专用测试 QQ 账号的 NapCat。先保留 Hermes 的模型配置和权限配置备份。不要把未知群成员直接接到具有宿主机终端、文件写入或浏览器权限的 Agent。

解压源码后，在 `hermes-napcat` 项目目录中执行：

```sh
uv sync --group dev
uv run pytest -q
```

仓库尚未附带在线解析生成的 `uv.lock`。首次 `uv sync` 会生成锁文件；在你的环境完成依赖解析与测试后，提交它以固定后续部署依赖。项目不自动下载 Hermes、QQ 客户端或 NapCat。

发行包另附 `requirements-tested.txt`，记录本次本地验收的依赖版本。该文件用于复现实验环境，不代替完整跨平台锁文件。

## 2. 配置 NapCat

在 NapCat 的 OneBot 网络配置中启用 **WebSocket Server**：

```text
host:              127.0.0.1
port:              3001
messagePostFormat: array
reportSelfMessage: false
enableForcePushEvent: true
token:             你生成的随机密钥
```

可参考 `examples/napcat-onebot-forward.json`。示例是需要合并的网络配置片段，不要覆盖原有完整 NapCat 配置。这里使用的是 **OneBot token**，与 NapCat WebUI 登录 token 无关。

生成密钥后，将同一个值填写到 NapCat OneBot 配置和 Hermes 所用 profile 的 `.env`：

```sh
uv run --no-project python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Hermes `.env` 示例：

```dotenv
NAPCAT_TOKEN=替换为上一步生成的密钥
NAPCAT_SELF_ID=机器人登录的QQ号
NAPCAT_ALLOWED_USERS=你的QQ号,另一个获准QQ号
NAPCAT_ALLOW_ALL_USERS=false
NAPCAT_HOME_CHANNEL=private:你的QQ号
```

`NAPCAT_ALLOWED_USERS` 同时供插件和 Hermes 网关鉴权读取。**部署时必须配置这项环境变量**；不要只在插件 YAML 中写 `allowed_users`，否则可能通过插件检查后被网关拒绝。空列表拒绝用户，`admins` 必须属于该列表。`NAPCAT_ALLOW_ALL_USERS=true` 会允许所有用户私聊调用，生产部署应保留 `false`。

令牌和 QQ 号不要提交到 GitHub。命令行诊断不会自动读取 Hermes 的 `.env`，运行诊断前请在当前进程环境中设置上述变量。Hermes 运行时由它自己的 profile secret scope 提供变量。

## 3. 安装到 Hermes

指定 **Hermes 实际使用的 Python 解释器**。`--hermes-python` 不应指向一个与 Hermes 无关的新虚拟环境。下面路径只是示例，须换成你的安装路径；Windows 使用对应虚拟环境中的 `Scripts/python.exe`。

```sh
uv run --no-project python scripts/install_plugin.py \
  --hermes-python /opt/hermes-agent/.venv/bin/python \
  --hermes-home /srv/hermes-home
```

安装脚本通过 `uv pip` 安装包，随后用该解释器运行真实 Hermes 接口检查，再将 `plugin/` 安装到 `<hermes-home>/plugins/napcat/`。它不会改写 Hermes 核心代码或现有配置文件。安装失败会返回非零退出码。已有同名目录时需要检查后使用 `--upgrade`；脚本会把旧入口备份到 `<hermes-home>/plugin-backups/`。

`--hermes-home` 必须与启动 Gateway 时使用的 `HERMES_HOME`/profile 目录一致。目录里存放的是导入入口，Python 包必须存在于运行 Gateway 的同一个环境中。多账号请使用各自独立的 Hermes profile、配置和缓存目录。

独立运行接口检查：

```sh
/opt/hermes-agent/.venv/bin/python scripts/check_hermes_contract.py
```

这个检查会导入实际 Hermes 类、验证抽象接口和注册参数；不会启动模型或连接 QQ。如果失败，请先处理版本/环境不匹配，不要绕过检查直接上线。

## 4. 合并 Hermes 配置

将 `examples/hermes.config.yaml` 中的相关键合并到现有 `config.yaml`，保留原有模型和其他平台配置。核心示例：

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
        self_id: '机器人QQ号'
        mode: forward
        ws_url: ws://127.0.0.1:3001
        allowed_groups: ['获准群号']
        admins: ['你的QQ号']
        group_mention: true
        group_reply_to_bot: true
        group_prefixes: ['/ai']
        group_toolsets: []
        media:
          outbound_roots: []
          shared_paths: []
platform_toolsets:
  napcat: []
```

先用无工具聊天验证收发。群聊默认用 `group_toolsets: []` 覆盖平台工具集；私聊由 `platform_toolsets.napcat` 决定。之后按你的 Hermes 实际工具集名称配置权限，不要照搬未经核验的工具集名称。

`admins` 允许网关控制命令和控制提示响应；不代表绕过 Hermes 自身权限。项目关闭平台 `/update` 能力。`group_sessions_per_user: true` 用于按群成员划分会话；更改为共享会话前要确认群成员愿意共享上下文。插件不会建立第二套会话数据库。

启动方式沿用你的 Hermes 部署，可以在专用工作目录下运行 `hermes gateway run`，也可以重启已经配置的 gateway 服务。

## 5. 分层验收

`examples/napcat.yaml` 是独立诊断使用的 **扁平配置**，内容对应 `gateway.platforms.napcat.extra`。修改其中的机器人号、管理员及群号，使其与实际环境一致。不要把完整 Hermes YAML 传给诊断 CLI。

```sh
uv run hermes-napcat --config examples/napcat.yaml check
uv run hermes-napcat --config examples/napcat.yaml probe
uv run hermes-napcat --config examples/napcat.yaml send \
  --target private:你的QQ号 --message 'NapCat transport test'
```

`check` 不访问网络；`probe` 核验已登录账号并读取 OneBot 状态；`send` 会向指定白名单目标发送一条真实消息。`probe` 能证明传输可用，不能证明 Hermes Gateway 已加载插件。

Gateway 启动后，先从白名单 QQ 私聊机器人，再在白名单群中发送 `@机器人 你好`、`/ai 你好` 和回复机器人消息。随后用未授权用户/群验证拒绝行为。测试真实 LLM、会话隔离和媒体的方法见 [TESTING](docs/TESTING.md)。

聊天目标使用 `private:123456789` 或 `group:987654321`，不接受没有类型前缀的数字。配置默认推送目标时，`NAPCAT_HOME_CHANNEL` 使用同一种写法。cron 的投递平台填写 `napcat`，由 Hermes 的目标解析与独立发送钩子完成路由。

## 6. 反向连接与容器网络

反向模式示例：

```yaml
mode: reverse
listen_host: 127.0.0.1
listen_port: 3002
ws_path: /onebot/v11
```

在 NapCat 启用 WebSocket Client，URL 设置为 `ws://127.0.0.1:3002/onebot/v11`，配置相同 token。可合并 `examples/napcat-onebot-reverse.json`。插件接受 Universal 双向连接，验证 `Authorization: Bearer ...`、账号和已连接客户端数量。

同一台物理机上的两个容器有各自的 loopback。NapCat 和 Hermes 位于不同容器时，连接地址应使用专用容器网络内的服务名，同时调整服务监听地址。显式设置 `allow_insecure_ws: true` 才允许非 loopback 明文连接；不要把 OneBot 端口公开到公网。跨主机建议通过受控隧道或校验证书的 WSS，反向监听需要由反向代理终止 TLS。代理须保留 Authorization/X-Self-ID/X-Client-Role，并关闭敏感头日志。

反向模式 `connect()` 成功表示监听器启动，NapCat 尚未连入时仍无法发送。该模式的独立 cron 发送返回错误；使用正在运行的 Gateway 投递，或改用正向模式。不要同时用诊断程序和 Gateway 抢占同一个反向监听端口。

## 7. 媒体和共享目录

默认只下载配置中列出的 QQ 图片/媒体主机，最大 10 MiB、单次最多 4 个附件，缓存预算 512 MiB。代码逐跳校验重定向，检查实际 DNS 解析结果，拒绝私网地址、代理环境变量、解压响应和超限传输。图片检查常见格式文件头，后续解码仍需依赖可信解码器。

URL 域名不在默认列表时，应根据你的真实 NapCat 事件添加精确域名，不使用通配符。只有专用只读媒体服务才能加入 `trusted_private_origins`，例如 `http://media-files:8080`；不要加入 NapCat 管理 API、云元数据服务或其他敏感内网服务。

出站文件必须位于显式允许的目录。小图片/音频/视频可设置：

```yaml
media:
  outbound_roots: ['/srv/hermes-output']
  inline_max_bytes: 524288
```

跨容器大文件或文档使用共享目录映射：

```yaml
media:
  shared_paths:
    - hermes: /srv/hermes-output
      napcat: /data/hermes-output
```

这两条路径必须由你挂载到同一存储内容。插件只做路径映射，不负责同步和挂载。NapCat 应对共享目录只读。文件上传使用 `upload_group_file`/`upload_private_file`；图片、语音和视频使用结构化 `file` 消息段。共享路径发送不会把整个文件读取为 base64。路径不存在、越界或符号链接指向允许目录外时，插件拒绝发送。

收到的 `file:///...` 或消息中的本地路径不会直接打开。语音/视频缺少可下载 URL、SILK 等格式需要额外解码、文件事件需要 NapCat 扩展查询时，本版会保留未读取说明，不能假装读到了附件。

## 8. 创建私有 GitHub 仓库

本项目附带人工运行的发布脚本。它要求本机已安装 GitHub CLI 和 git，不会读取聊天中的 token，也不会修改全局 git 身份。

在解压后的项目目录执行：

```sh
gh auth login --hostname github.com --scopes workflow
uv run --no-project python scripts/publish_github.py --repo ii999/hermes-napcat
```

脚本确认当前 gh 登录账号与仓库 owner 一致，初始化 `main`、只暂存项目文件，然后执行 `gh repo create --private --source ... --remote origin --push`。它拒绝更改已有 remote，不会覆盖现有仓库，不会创建公开仓库。已有仓库请自行检查 remote 并按常规 git 流程提交。组织仓库不在该脚本首版范围内。

GitHub Actions 配置包含 Python 3.12/3.13 测试、静态检查和构建。本地生成源码包时并未运行远程 Actions。

## 开发与故障排查

```sh
uv run pytest -q
uv run ruff check .
uv build
```

常见问题：能探测但 Agent 无回复时检查插件是否加载、profile 目录是否一致以及 `NAPCAT_ALLOWED_USERS`；收群消息但不触发时检查用户与群白名单、@目标和前缀边界；发送结果为 `delivery_uncertain` 时先检查 QQ 会话，避免手工重试重复发出；附件拒绝时检查域名、字节上限与共享挂载。日志不会主动输出原始消息或令牌，但部署者仍需限制日志访问并对上游错误做脱敏。

[架构说明](docs/ARCHITECTURE.md) · [上游兼容性](docs/COMPATIBILITY.md) · [安全边界](SECURITY.md) · [开发计划](docs/ROADMAP.md)
