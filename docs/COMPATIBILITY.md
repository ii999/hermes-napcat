# 上游接口核验

接口基准为 Hermes `v2026.9.14`；2026-10-08 的媒体接口补充见下文。源码核验、模拟协议检查与真实环境验收分开记录，均不能相互替代。当前没有真实 QQ/完整 Hermes Gateway 媒体联调结论。

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

目录插件暴露 `__init__.py:register(ctx)`。`register_platform` 使用 PlatformEntry 字段，包含目标解析、独立 sender、授权环境变量和默认投递目标。standalone sender 支持 `media_files` 的 `(local_path, is_voice)` 列表和 `force_document`；预检整个组合后投递。仍拒绝 native thread，反向独立 sender 需改用实时 Gateway。

配置读取使用 `get_scoped_secret`，不在插件里写进程全局环境。私聊及未开放群的成员允许名单须同时让 Hermes 的 gateway 鉴权可见。全员授权群通过 `extra.group_allowed_chats` 传递给 Hermes 原生群授权分支：适配器发布规范化的 `group:<id>` 字符串列表，所有项必须也在插件的 `allowed_groups` 中，拒绝通配符和私聊目标。该原生分支只对群类会话生效，优先于普通用户白名单；配置属于实际接收消息的适配器/profile，不修改全局环境。群会话工具限制采用 toolsets_for_source()。`scripts/check_hermes_contract.py` 增加原生群授权 helper 的群/私聊隔离检查；本次仅完成源码核验及模拟插件测试，未在真实安装中执行该脚本。源码检查不能证明外部 profile 路由、memory 或其他 Hermes 插件不会改变行为，部署验收需要覆盖这些组合。

QQ 工具通过公开 `ctx.register_tool()` 注册，在目录插件 manifest 声明发现信息，适配 deferred platform loading。handler 使用 `gateway.session_context.get_session_env()` 取得 task-local 来源；action 调度读取 `gateway.run._gateway_runner_ref` 和 runner `_authorization_adapter()`，后两者是 Hermes 内部接口。升级后必须重新执行接口检查及真实工具调用。读工具现在属于 `napcat_qq_read`，发送属于 `napcat_qq`；旧配置要显式增加读工具集。

群调度使用 `on_processing_start()` 跟踪后台任务、内部 `_gateway_accepted` 标记、`should_bypass_active_session()` 以及 `_event_session_key()`/`get_pending_for_session()` 澄清匹配。升级时需复核。后台排队、上下文切换和关闭取消有模拟覆盖，尚未完成真实 Gateway 联调。

原生受邀续聊复用 `BasePlatformAdapter.set_message_handler(handler)` 注册的处理器；`on_processing_start()` 将真实后台处理任务绑定到当前候选。处理器返回结构化决定，插件校验后直接发送纯文本，并返回 `None` 抑制宿主的最终附件解析与发送。已源码核验 [run_turn.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/gateway/run_turn.py) 中的进度、流式、语音投递可能发生在处理器返回前，因此发送门禁覆盖这些路径，且媒体预处理入口另行拒绝。QQ 适配器声明不支持编辑和原生流式；不能只依赖该声明阻止中间输出。Hermes 可能在插件校验前持久化结构化结果。该路径有模拟生命周期测试，尚未通过真实 Gateway/模型/QQ 验收。

Hermes 的 `/queue` 路径可能在处理器返回前投递首个结果。插件覆盖公开 `send_final_ledgered()`，在创建可恢复投递记录之前拒绝原生续聊的内部结果，防止重启补投递泄漏决策 JSON。宿主通过 `on_processing_start()` 切换到已授权的排队请求后，插件按实际终结消息处理普通回复。此处依赖宿主生命周期回调，升级时须复核；模拟测试覆盖投递记录拦截、无回合上下文的恢复及排队回复保留。插件直接发送的续聊正文不进入 Hermes 最终回复投递账本；失败由插件日志和计数器记录，宿主回合完成不代表 QQ 已投递成功，也不会据此重发续聊正文。

## 媒体理解与缓存接口（2026-10-08，源码核验）

| 依赖 | 已核验的源码接口 | 真实环境边界 |
| --- | --- | --- |
| 图片/视频 | [registry](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/registry.py) availability/dispatch 和 [vision_tools](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/vision_tools.py) 注册 handler | native/aux 选择、模型支持和视频依赖须实测 |
| 转录 | [transcription_tools](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/transcription_tools.py) 公共转录与 installed-only fallback；插件检查 runner STT 开关 | 提供商/本地后端和 MP3 转换须实测 |
| 文档 | [file_tools](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/file_tools.py) `read_file` 与 [read_extract](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/read_extract.py) 支持格式/50 MiB 限制 | 行分页不限制完整提取成本；sandbox 文件可见性须实测 |
| 缓存 | [hermes_constants](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/hermes_constants.py) `get_hermes_dir` 与 [credential_files](https://github.com/NousResearch/hermes-agent/blob/v2026.9.14/tools/credential_files.py) agent-visible 映射 | 使用标准 documents 父目录/旧 document_cache 下的 napcat；映射不保证字节同步 |

`qq_read_media` 通过公开 registry handler 保留原生多模态结果，普通文档页默认 200 行、最多 500 行，普通字符串结果最多 16,000 字符。缺失能力明确失败。这些是源码和接口替身验证，当前没有导入用户真实 Hermes 安装或运行模型/STT/视频解析。

NapCat 下载核验当前 `main` 的 [BaseDownloadStream](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/BaseDownloadStream.ts)、[图片](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/DownloadFileImageStream.ts)、[语音](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/DownloadFileRecordStream.ts)、[文件](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/stream/DownloadFileStream.ts)、[OneBot 封装](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/OneBotAction.ts) 和 [WebSocket 服务端](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/network/websocket-server.ts)。一个 echo 多个 `stream-action` envelope，info/chunk/complete；unsupported API 为 failed/1404/normal-action，下载错误为 stream-action。语音请求 MP3，初始大小可能仍是原文件大小。源码 `main` 不代表固定已验收发行版。

历史/转发来源核验依据 [GetFriendMsgHistory](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/GetFriendMsgHistory.ts)、[GetForwardMsg](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/msg/GetForwardMsg.ts) 和 [file UUID 生命周期](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-common/src/file-uuid.ts)。短期 ID 失效仍可能发生；刷新一次不承诺恢复。`qq_get_forward` 只接受已核验父消息 ID，原样服务端重发沿用 `qq_send_forward` 的已有消息节点。

## 历史媒体扩展记录（2026-09-30）

当时基于插件 `741820f` 增加 `qq_get_media`，沿用 session 绑定及 `MessageEvent.media_urls/media_types`。路径 JSON 不自动成为视觉输入。该记录不代表真实 Gateway、模型、QQ 或共享卷已验收，当前行为以 [MEDIA](MEDIA.md) 为准。

当时验证了两方向 URL 策略、`get_image` URL 刷新、多图 WS 预算和分块上传，当时尚未实现独立 cron 媒体发送。当前正向组合投递已实现，真实 cron 仍待验收。

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

流式上传的当前核验基准为 NapCatQQ `26d7533e0f5800fdff865ab2f2ad7692917e1076`，请求/回执、reset 特性及上游内存合并限制见 [STREAM_UPLOAD](STREAM_UPLOAD.md)。这属于源码核验和模拟服务协议测试，没有宣称真实 QQ 已验收。
