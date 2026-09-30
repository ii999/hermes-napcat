# 上游接口核验

核验日期：2026-09-18。这里区分源码核验与运行验收，不将官方接口文档视为本项目真实上线测试结果。

## Hermes

核验基准：GitHub release `v2026.9.14`，展示版本 0.21.3。

- Release: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.14
- 平台接口: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/platforms/base.py
- 入站事件: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/platforms/event.py
- 平台注册字段: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/platform_registry.py
- Profile 密钥读取: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/platforms/_shared.py
- 插件加载器: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/hermes_cli/plugins.py
- 工具注册: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/registry.py
- Gateway session context: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/session_context.py
- Profile adapter 解析: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/authz_mixin.py
- SessionSource: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/session.py
- 配置: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/config.py
- 开发指南: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/website/docs/developer-guide/adding-platform-adapters.md

本项目实现 connect(*, is_reconnect=False)、disconnect、send、get_chat_info 和部分媒体发送接口。入站使用 MessageEvent.source，而不是把 platform/chat_id 当作 MessageEvent 的独立字段。Adapter 通过 BasePlatformAdapter.build_source() 构造来源。

目录插件暴露 `__init__.py:register(ctx)`。`register_platform` 使用 PlatformEntry 字段，包含目标解析、独立 sender、授权环境变量和默认投递目标。standalone_sender_fn 的签名包含 thread_id、media_files、force_document；本版明确拒绝线程和独立媒体请求。

配置读取使用 `get_scoped_secret`，不在插件里写进程全局环境。私聊/群成员允许名单须同时让 Hermes 的 gateway 鉴权可见。群会话工具限制采用 toolsets_for_source()。源码检查不能证明外部 profile 路由、memory 或其他 Hermes 插件不会改变行为，部署验收需要覆盖这些组合。

0.2.0 的五个 QQ 工具通过公开 `ctx.register_tool()` 注册，并在 `plugin.yaml` 声明 `provides_tools`，适配 Hermes 的 deferred platform loading。handler 使用 `gateway.session_context.get_session_env()` 取得 task-local 来源。为了把 action 调度到持有 WebSocket 的 Gateway loop，并按 profile 选择正确 adapter，当前实现还读取 `gateway.run._gateway_runner_ref` 并调用 runner 的 `_authorization_adapter()`；这两个运行时入口属于 Hermes 内部接口。`scripts/check_hermes_contract.py` 会检查本版依赖的形状，但升级 Hermes 后仍必须执行真实工具调用验收。

启用群聊上下文后增加第六个工具 `qq_get_recent_messages`。群调度使用 `on_processing_start()` 跟踪后台任务，并读取 Hermes 的内部入队标记 `_gateway_accepted`；控制命令绕行调用 `should_bypass_active_session()`，澄清回答通过 `_event_session_key()` 与 `tools.clarify_gateway.get_pending_for_session()` 匹配当前会话。升级 Hermes 时需复核这些接口。后台排队、任务上下文切换和关闭取消有模拟测试覆盖，尚未完成真实 Hermes Gateway 联调。

## 本次媒体扩展（2026-09-30）

基于插件 `741820f` 增加第七个 QQ 工具 `qq_get_media`，沿用现有工具注册与 session 绑定接口；正常媒体事件使用既有 `MessageEvent.media_urls/media_types`。工具 JSON 中的路径不会自动成为视觉输入。没有重新宣称某个更新的 Hermes/NapCat 发行版已实测；真实 Gateway、视觉模型、QQ 客户端、共享目录权限和 10 MiB/16 MiB 配置仍须部署验收。

两方向 URL 策略、一次 `get_image` URL 刷新和普通多图 WS 预算在本地替身/loopback 测试中验证。不会打开 NapCat 返回的本地路径，不提供流式上传或独立 cron 图片发送。详细迁移规则与边界见 [MEDIA](MEDIA.md)。

## NapCat

网络 schema 与客户端代码核验的是 2026-09-18 获取的 main，不把它当作已在某个发行版上完成实机测试：

- Schema: https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/config/config.ts
- Schema blob SHA: `71e1edcf5b3372c70817936a9365b5ece680702a`
- WS client: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-onebot/network/websocket-client.ts
- 文档: https://napneko.github.io/onebot/api
- 消息段: https://napneko.github.io/develop/msg
- 合并转发实现: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-onebot/action/msg/SendMsg.ts
- 文件上传实现: https://github.com/NapNeko/NapCatQQ/tree/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-onebot/action/go-cqhttp

schema 包含 websocketServers / websocketClients、array 消息格式、token、reportSelfMessage、heartInterval、reconnectInterval；正向服务端还有 enableForcePushEvent。反向客户端发送 Authorization Bearer、X-Self-ID 和 Universal role，与本插件鉴权一致。

NapCat `v4.18.28` 的发送 schema 支持 node 消息、`source/news/summary/prompt` 扩展字段，以及图片、语音、视频、文件段。`upload_group_file` 和 `upload_private_file` 接受本地路径、HTTP(S) URL 或 base64；插件仍先在 Hermes 侧检查 URL，只把已验证内容转换成 base64 或共享路径交给 NapCat。

本项目未包含 NapCat/QQ 运行时，没有固定已实测的 NapCat 构建号。部署者应固定自己的 QQ 与 NapCat 版本，并在 TESTING 的验收表中记录实际结果。对其他 OneBot v11 实现仅声明协议层可能复用，不承诺即插即用兼容所有媒体扩展。

## 升级约束

升级 Hermes 后先在其真实 Python 环境运行 scripts/check_hermes_contract.py，再做收发、权限、会话和媒体验收。通过接口签名检查仍可能遇到行为变更。本版不提供对老版缺失插件字段的静默降级，也不会退回源码 patch。
