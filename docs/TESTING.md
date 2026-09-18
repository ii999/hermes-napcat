# 测试与验收记录

日期：2026-09-18；版本：0.1.0。运行环境为本次交付容器，不是用户的 QQ 服务器。

## 已执行

在 Linux、Python 3.13.5 环境执行以下命令，**76 passed，0 failed**：

```sh
PYTHONPATH=src uv run --no-project --offline python -m pytest -q
PYTHONPATH=src uv run --no-project --offline python -m compileall -q src scripts plugin tests
uv build --offline --no-build-isolation
```

本地构建成功产生 sdist 和 wheel。使用 --no-project 的测试命令是为了复用已有依赖、避免当前容器的外网限制；正常开发环境按 README 使用 uv sync / uv run。验收依赖版本记录在 requirements-tested.txt。

76 项包含参数化用例。协议测试覆盖账号/消息 ID 区别、CQ 注入隔离、前缀边界、文本分段、白名单交集、管理员关系、去重及限流。第一次分段测试发现换行位置恰好位于长度上限时产生超长分段，代码已修复并通过复测。

网络测试使用真实 aiohttp TCP loopback 连接和模拟 OneBot 服务端，覆盖正向/反向连接、Bearer/账号/role 校验、单反向连接限制、并发乱序 echo、事件回调内部发起 action、超时清理、迟到响应、断线后重连、有界事件队列、同聊天顺序、跨聊天并行和超大 WS 帧断连。

媒体测试使用真实本地 HTTP 服务，覆盖允许的专用 origin、重定向目标复验、循环重定向、Content-Length 与流式大小限制、伪装图片、编码响应拒绝、临时文件清理、缓存权限及额度。DNS 测试使用 resolver 返回值替身，核验混合公网/私网结果被拒绝。路径测试覆盖目录外文件、符号链接越界、inline 上限和 Windows 远端路径映射。

适配器测试使用按上游源码签名编写的 Hermes 接口替身，覆盖 ACL 检查先于网络/媒体访问、SessionSource 字段、管理员控制、去重、引用来源校验、关闭媒体时不发起下载、群工具集限制、分段发送、CQ 字符串纯文本处理、部分发送失败、文档上传前校验和插件注册。

发布脚本测试模拟 gh/git 命令结果，核验账号限制、private 默认、已有 remote 拒绝、显式暂存目录、无嵌套 shell 和 github.com 主机选择。这些测试没有调用真实 GitHub 写接口。

## 未执行

本地没有 ruff 且离线缓存中没有其发行包，因此未运行 ruff，也未生成在线解析的 uv.lock。已提供 GitHub Actions 的 Python 3.12/3.13、pytest、ruff 和构建任务，但仓库尚未创建，远程 CI 未运行。Python 3.12 和 Windows 运行测试未执行。

当前容器没有完整 Hermes 安装、已登录 QQ 的 NapCat 或用户模型凭据。没有执行真实 Hermes 插件加载、完整 Gateway、模型/Memory/Skills、cron、STT/TTS 和 QQ 的端到端验收。接口替身测试和源码核验不能替代这些验收。

当前 GitHub 连接可读账号、操作已授权仓库内容，但没有新建仓库动作；未创建远程仓库、未推送代码。scripts/publish_github.py 供已登录 gh 的操作者创建私有仓库并发布。

## 部署机验收步骤

1. 用实际 Hermes Python 运行 scripts/check_hermes_contract.py，确认导入、抽象接口和平台注册通过；再启动 Gateway，确认插件被发现。
2. 在 flat CLI 配置上运行 check/probe，确认预期 bot self_id；由操作者发送一条诊断私聊并在 QQ 检查。
3. 测试白名单私聊、群 @、回复、/ai；验证其他群和其他用户不会触发模型。检查非管理员 /new 等控制指令不会执行。
4. 让同群两个用户分别提供不同信息，再查询各自上下文；确认 group_sessions_per_user 的真实行为。多个 profile 分别重复测试。
5. 发送图片、中文长消息、文档及实际音视频。检查附件缺 URL/格式不支持时会显示未读取说明。将 QQ 与 NapCat 的实际版本写入验收记录。
6. 停止 NapCat、恢复连接、重启 Gateway，检查断线行为、去重边界与权限。发送期间断网后先检查 QQ，再决定是否人工重试。
7. 用 Hermes cron 配置 napcat 文本投递，验证目标解析与独立 sender；反向模式只验收正在运行的 Gateway 路径。

验收前保持模型工具集为空。每增加一种工具、共享目录或私人媒体 origin，都应重新检查权限边界。
