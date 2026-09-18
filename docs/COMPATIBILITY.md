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
- SessionSource: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/session.py
- 配置: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/config.py
- 开发指南: https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/website/docs/developer-guide/adding-platform-adapters.md

本项目实现 connect(*, is_reconnect=False)、disconnect、send、get_chat_info 和部分媒体发送接口。入站使用 MessageEvent.source，而不是把 platform/chat_id 当作 MessageEvent 的独立字段。Adapter 通过 BasePlatformAdapter.build_source() 构造来源。

目录插件暴露 `__init__.py:register(ctx)`。`register_platform` 使用 PlatformEntry 字段，包含目标解析、独立 sender、授权环境变量和默认投递目标。standalone_sender_fn 的签名包含 thread_id、media_files、force_document；本版明确拒绝线程和独立媒体请求。

配置读取使用 `get_scoped_secret`，不在插件里写进程全局环境。私聊/群成员允许名单须同时让 Hermes 的 gateway 鉴权可见。群会话工具限制采用 toolsets_for_source()。源码检查不能证明外部 profile 路由、memory 或其他 Hermes 插件不会改变行为，部署验收需要覆盖这些组合。

## NapCat

网络 schema 与客户端代码核验的是 2026-09-18 获取的 main，不把它当作已在某个发行版上完成实机测试：

- Schema: https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/config/config.ts
- Schema blob SHA: `71e1edcf5b3372c70817936a9365b5ece680702a`
- WS client: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-onebot/network/websocket-client.ts
- 文档: https://napneko.github.io/onebot/api
- 消息段: https://napneko.github.io/develop/msg

schema 包含 websocketServers / websocketClients、array 消息格式、token、reportSelfMessage、heartInterval、reconnectInterval；正向服务端还有 enableForcePushEvent。反向客户端发送 Authorization Bearer、X-Self-ID 和 Universal role，与本插件鉴权一致。

本项目未包含 NapCat/QQ 运行时，没有固定已实测的 NapCat 构建号。部署者应固定自己的 QQ 与 NapCat 版本，并在 TESTING 的验收表中记录实际结果。对其他 OneBot v11 实现仅声明协议层可能复用，不承诺即插即用兼容所有媒体扩展。

## 升级约束

升级 Hermes 后先在其真实 Python 环境运行 scripts/check_hermes_contract.py，再做收发、权限、会话和媒体验收。通过接口签名检查仍可能遇到行为变更。本版不提供对老版缺失插件字段的静默降级，也不会退回源码 patch。
