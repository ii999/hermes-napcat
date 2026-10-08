# 测试与验收记录

模拟协议、上游源码核验与真实环境验收分别记录。历史结果只适用于其日期/提交和当时环境；不代表当前代码已在真实 QQ 或完整 Hermes Gateway 验收。

## 媒体管线验证范围（2026-10-08）

| 范围 | 覆盖行为 | 不证明的内容 |
| --- | --- | --- |
| loopback WebSocket/HTTP | 多响应 echo 与普通 action 交错、帧/缓冲/字节上限、规范 base64、块顺序/总数、转换前后音频大小、取消/超时/断线清理、旧 epoch 响应、HTTP 策略 | 真正 NapCat/QQ 文件可用性、风控及播放编码 |
| 协议/适配器替身 | 四类引用、来源/过期/撤回、当前加引用预算、失败 allowance、私聊历史/转发父消息、通知 ACL/去重、读写权限分离、组合投递预检/部分结果 | 实际 Hermes 插件发现、profile 路由、Gateway 调度和 cron |
| Hermes handler 替身 | native multimodal 保留、STT 开关/本地 fallback、视频/文档 dispatch、文档结果限制及可见路径映射 | 真模型理解、STT/FFmpeg/文档依赖、sandbox 字节同步 |
| 上游源码 | Hermes `v2026.9.14` registry/cache/analysis 与当前 NapCat stream/history/forward 实现 | 发布版兼容承诺或真实安装验收 |

源码链接及版本边界见 [COMPATIBILITY](COMPATIBILITY.md)。当前没有安装用户的真实 Hermes、登录 QQ 的 NapCat 或模型凭据进行完整联调。正式结果应附实际提交、运行环境和以下项目检查输出：

```sh
uv run pytest -q
uv run ruff check .
uv build
```

## 历史：PR #3 审查回归（2026-09-30）

macOS/Python 3.12.13 使用项目依赖运行 `uv run pytest -q`，**229 passed**；`uv run ruff check .`、`uv build` 和 `git diff --check` 均通过。新增回归覆盖带首尾空白的 base64/data URI 批次预算、私聊及群聊图片在等待 WebSocket 写锁期间撤回或过期，以及上传返回路径混用斜杠时的父目录跳转和 UNC 拒绝。

WebSocket 测试使用本地 loopback 服务及 Hermes/OneBot 替身。上游流式接口按固定提交做源码核验；未执行真实 NapCat/QQ 或完整 Hermes Gateway 联调。

## 历史：流式上传与 base64 扩展回归（2026-09-30）

本轮基于 PR #3 的 `fce774b`，在 Linux/Python 3.13.5 使用 `uv run --no-project --offline pytest -q` 复现原有 171 项通过，再运行扩展测试得到 **218 passed**。`uv build --offline --no-build-isolation` 成功生成源码包和 wheel。本地没有 Ruff；本轮代码提交 `4aa43e6` 已通过 Python 3.12/3.13 GitHub Actions 的 `uv sync --group dev`、pytest、Ruff 和构建，见 [CI #27](https://github.com/ii999/hermes-napcat/actions/runs/36708938990)。后续提交以 PR 最新 checks 为准。

新增 47 项参数化回归覆盖标准 base64/data URI、旧 8192 字符限制修正、规范编码/MIME/大小/总批次预算、直接与缓存传输、四种媒体分块发送、逐块与完整性回执、错误路径/哈希/大小、失败/取消/关闭、不重复发 QQ、连接 epoch/保留期、旧 WS 限制动态分块、正反向真实 loopback WebSocket、消息队列总字节预算和撤回独立通道。上游接口源码基准与实机边界见 [STREAM_UPLOAD](STREAM_UPLOAD.md)。未执行真实 QQ 或完整 Hermes Gateway 联调。

## 历史：媒体扩展回归（2026-09-30）

基线 `741820f` 的本地测试为 126 项。新增分方向策略、共享暂存、链接刷新、受控引用及 WS 拆分后，在 Linux/Python 3.13.5 本地执行 `uv run --no-project --offline pytest -q`，**171 passed**。本机复用已安装的测试依赖；该离线命令不等同于锁文件重建环境。`uv build --offline --no-build-isolation` 成功生成 sdist 和 wheel，并包含新增媒体模块。

新增回归覆盖旧配置继承、公网模式拒绝非公网目标、两方向私网信任隔离、逐跳重定向、共享暂存权限与额度、URL 失效一次刷新、不读取 NapCat 路径、短期引用的账号/会话/期限/撤回/容量限制、同发言人近期补图、无工具引用补图、主动轮次不读图、撤回竞态、WebSocket 撤回通知的独立有界处理、排队发送时失效、UTF-8 多图整批预检及部分成功。网络测试使用 loopback 服务，其余使用 Hermes/action/DNS 替身；不是 QQ 或真实 Gateway 联调。

本地没有 Ruff 可执行文件；标准 `uv sync --group dev`、`uv run pytest -q`、`uv run ruff check .` 和 `uv build` 由本 PR 的 Python 3.12/3.13 GitHub Actions 验证，以具体提交的 CI 结果为准。CI 保留 JUnit 报告及构建包 7 天。真实部署验收补充见 [MEDIA](MEDIA.md)。

以下保留 2026-09-18 的历史验收记录，其中的“未执行”指当次环境，不覆盖上述新增本地/CI 记录。

## 历史：原版已执行（2026-09-18）

在 Linux、Python 3.12.14 环境执行以下命令，**86 passed，0 failed**，Ruff 检查通过：

```sh
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts plugin tests
uv build
PYTHONPATH=/path/to/hermes-agent:src uv run python scripts/check_hermes_contract.py
```

本地构建成功产生 0.2.0 的 sdist 和 wheel，wheel 包含新增的 `hermes_napcat.tools`。仓库已生成 `uv.lock`，本次环境中的直接测试依赖版本另记录在 `requirements-tested.txt`。

86 项包含参数化用例。协议测试覆盖账号/消息 ID 区别、CQ 注入隔离、前缀边界、文本分段、白名单交集、管理员关系、去重及限流。配置测试确认 Agent QQ 工具默认关闭且各项上限受约束。

网络测试使用真实 aiohttp TCP loopback 连接和模拟 OneBot 服务端，覆盖正向/反向连接、Bearer/账号/role 校验、单反向连接限制、并发乱序 echo、事件回调内部发起 action、超时清理、迟到响应、断线后重连、有界事件队列、同聊天顺序、跨聊天并行和超大 WS 帧断连。

媒体测试使用真实本地 HTTP 服务，覆盖允许的专用 origin、重定向目标复验、循环重定向、Content-Length 与流式大小限制、伪装图片、编码响应拒绝、临时文件清理、缓存权限及额度。DNS 测试使用 resolver 返回值替身，核验混合公网/私网结果被拒绝。路径测试覆盖目录外文件、符号链接越界、inline 上限、Windows 远端路径映射，以及只允许未变化的缓存自有文件转换为 base64。

适配器测试使用按上游源码签名编写的 Hermes 接口替身，覆盖 ACL 检查先于网络/媒体访问、SessionSource 字段、管理员控制、去重、引用来源校验、关闭媒体时不发起下载、群工具集限制、分段发送、CQ 字符串纯文本处理、部分发送失败、文档上传前校验和插件注册。

Agent QQ 工具测试覆盖图文顺序和引用核验、跨会话的开关/管理员/目标 ACL 三重检查、文件上传后的部分失败、已有消息与多媒体自建节点混合转发、外部会话引用在发送前拒绝、同一 action 并发合并、读取结果不暴露媒体 URL，以及 profile 开关关闭时失败关闭。

真实 Hermes 源码检查使用 `v2026.9.14`（0.21.3），验证 adapter 非抽象、平台注册、五个工具注册、session context API 和按 profile 解析 adapter 所需的接口形状。该检查导入真实 Hermes 模块，但不启动 Gateway 或 QQ。

## 历史：原版未执行（2026-09-18）

当前容器没有完整 Hermes 安装、已登录 QQ 的 NapCat 或用户模型凭据。没有执行真实 Hermes 插件加载、完整 Gateway、模型/Memory/Skills、cron、STT/TTS 和 QQ 的端到端验收。接口替身测试和源码核验不能替代这些验收。

没有执行 Python 3.13、Windows 或真实 QQ 环境测试。本次本地记录不替代 GitHub Actions，也不证明 NapCat 当前构建与 QQ 客户端的多媒体编码组合可用。

## 部署机验收步骤

1. 用实际 Hermes Python 运行 scripts/check_hermes_contract.py，确认导入、抽象接口和平台注册通过；再启动 Gateway，确认插件被发现。
2. 在 flat CLI 配置上运行 check/probe，确认预期 bot self_id；由操作者发送一条诊断私聊并在 QQ 检查。
3. 测试白名单私聊、群 @、回复、/ai；验证其他群和其他用户不会触发模型。检查非管理员 /new 等控制指令不会执行。
4. 让同群两个用户分别提供不同信息，再查询各自上下文；确认 group_sessions_per_user 的真实行为。多个 profile 分别重复测试。
5. 私聊/群聊分别发送图片、语音、视频和文档，测试 `offline_file`/`group_upload`、当前附件加引用共享预算、先发附件后问、过期/撤回及失败下载占用 allowance。检查 `auto`/`stream`/`http`、unsupported 和文件失效一次刷新；畸形/超额/断线不得回退 HTTP。记录真实 QQ/NapCat 版本。
6. 停止 NapCat、恢复连接、重启 Gateway，检查断线行为、去重边界与权限。发送期间断网后先检查 QQ，再决定是否人工重试。
7. 正向 Hermes cron 测试文本/本地图片/voice/视频/文件组合及 `force_document`；无效末项须在首条 QQ 消息前拒绝。模拟中途断线核对已知 ID、partial/uncertain，无自动重发。反向独立 sender 须拒绝，实时 Gateway 另验收。
8. 先仅授权 `napcat_qq_read` + `read_enabled`：读取当前/私聊/群历史、父消息绑定的嵌套转发及四类媒体；验证不能发送、跨 profile/会话引用/任意 forward ID 被拒绝。再加 `napcat_qq` + `enabled`，测试图文交错、服务端已有消息转发与 partial/uncertain。
9. 调用 `qq_read_media`，分别验收 native/aux 视觉、STT 开/关及 installed-only fallback、视频 handler、PDF/Office/文本分页/截断和缺依赖显式失败。用实际 sandbox 读取新缓存，确认 profile 新/旧 document cache 路径映射及挂载同步。
10. 重复多账号/profile、群成员隔离与撤回竞态检查。把成功/失败、版本、模型、解析依赖、挂载方式和日期写入部署记录，不把下载成功记为内容理解成功。

基础验收保持模型工具集为空，随后先开只读，再按需要开发送。每增加工具、共享目录或私有媒体 origin，都重新检查权限边界。
