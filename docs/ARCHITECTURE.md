# 实现架构

## 模块职责

`config.py` 校验配置；`protocol.py` 负责消息段、目标和标识符；`policy.py` 负责白名单、触发、限流和去重；`transport.py` 负责认证 WebSocket、普通 action/echo 和有界多响应流。`stream_download.py` 验证固定 NapCat 下载协议，`media.py` 负责字节、配额、原子缓存、HTTP 安全与共享暂存，`stream_upload.py` 负责上传完整性和连接绑定。`media_refs.py` 保存四类媒体的来源/期限，`media_adapter.py` 统一读取/发送边界，`history.py` 负责私聊历史与父消息绑定的合并转发，`file_notices.py` 规范化文件通知并有界去重。

`adapter.py` 构造 Hermes 事件及受控动作；`media_understanding.py` 在主机边界调用已安装的 Hermes handler；`tools.py`/`group_tools.py` 绑定当前 session 和读写工具；`outbound.py` 预检主机组合投递；`plugin.py` 注册平台/投递钩子；`cli.py` 提供诊断。协议、存储、流式传输和历史模块不导入 Hermes。

可选群聊层由 `group_adapter.py` 扩展基础适配器。`context.py` 保存有界观察记录，`group_chat.py` 负责观察、回填和调度，`engagement.py` 负责规则/无工具模型参与判断，`group_tools.py` 注册基础工具及近期群消息读取工具。后四个模块中的协议和调度逻辑不导入 Hermes；只有适配器和工具执行边界使用主机接口。

目录插件 `plugin/__init__.py` 暴露 `register(ctx)`，`plugin/tools.py` 为 Hermes 的延迟平台加载器提供独立 `register_tools(ctx)`。实现代码装入 Hermes 的 Python 环境。工具发现不会导入 adapter 或连接 QQ；平台加载时重复注册采用 Hermes 的同一插件作用域。

## 入站与鉴权

OneBot reader 先区分事件和 API 响应。连接后调用 `get_login_info` 核对账号；确认前仅缓存有界事件。普通 echo 完成 Future，多响应下载 echo 进入专用有界队列；饱和只使该流失败，不阻塞普通 action 或事件 worker。文件通知进入正常事件通道，撤回有独立有界通道。事件 handler 可调用 `get_msg` 等 action。

基础模式下，worker 按聊天键顺序进入 adapter，跨聊天最多并行 event_workers 个。插件先检查账号、用户、群；再处理去重、限流和触发。群回复触发仅接受本进程记得的机器人消息，或通过 `get_msg` 验证同一群、机器人作者的消息。模型或用户提供的 quote sender 字段不能作为授权依据。

插件随后处理允许的媒体并调用 `build_source()`，保留用户身份、聊天类型、消息 ID 和账号 scope 元数据，通过 `handle_message()` 交给 Hermes。用户/群 ID 不复用同一裸数字地址。实际会话键、profile 路由、memory、模型调用及 cron 调度由 Hermes 负责。多个机器人账号应分配独立 profile；不依赖未验证的跨账号 session-key 推断。

网关鉴权仍然启用。插件不会设置 `internal=True` 或伪造 `role_authorized=True`。非管理员事件设置 `allow_gateway_control=False`。群聊工具集默认用空列表覆盖；私聊使用 Hermes 平台工具配置。

## 可选群聊上下文与参与

启用 `group_context` 后，群消息先进入独立的观察控制器。观察缓存保留有界文本、发言人、时间、引用及附件类型，媒体引用开启后另保留短期 `media_id`；执行队列另行处理获准用户的直接请求，等待模型期间仍能观察新消息。默认只观察授权用户，管理员可单独开启 `observe_all_members`，但观察权限不会授予 Agent 或工具权限。

被点名时，控制器按需通过已有 WebSocket 查询近期历史、校验同群归属和引用来源，再以 `MessageEvent.channel_context` 注入本轮背景。它保留真实 `source.user_id`、当前文本和账号 scope，不把个人会话合并为共享群会话，也不将历史逐条重放成命令。连接变化或事件缺口会触发受限回填，无法保证完整恢复离线期间的消息。

Hermes 的 `handle_message()` 只完成入队。适配器通过 `on_processing_start()` 关联实际后台任务，在任务结束后释放群队列和跨群处理名额；同群主动与直接请求共用执行锁。每个后台任务按消息重新绑定主动回复标记，防止 Hermes 排队任务继承上一轮的标记。管理员控制命令及待处理澄清问题的回答绕过等待队列。历史查询的清理由查询任务完成回调负责，取消等待者不会阻止后续重连回填。

`proactive_assist` 默认关闭，启用后默认 dry-run。控制器合并同一发言人的连续短句，等待静默窗口，以规则或独立无工具分类器决定是否参与，并限制冷却、模型判断次数和发言次数。实际主动回复要求 `group_toolsets: []`，继续使用获准发言人的真实身份且关闭网关控制权限。第一段发送前再次核对群状态与过期时间，发送开始后保留原有部分成功和不确定投递语义。

启用后的 `group_recall` 通知会移除缓存并创建有界 tombstone，防止回填恢复已撤回文本。已提交给 Hermes 或模型提供方的内容无法靠缓存撤回收回。配置、数据保留和实际环境验收边界见 [GROUP_CHAT](GROUP_CHAT.md)。

## 发送语义

发送目标先经 `private:<QQ>` / `group:<ID>` 解析和 ACL 校验，模型回复作为纯 text 消息段。引用使用独立 reply 段。分段过程保持文本内容并限制每段长度，只有第一段带引用。每次 OneBot action 使用新 echo；成功必须收到 `status=ok` 且整数 `retcode=0`。

`status=async` 是受理，不表示完成。发送断开、写后超时或缺失 message_id 表示结果不确定，禁止自动重发。多段发送失败保留已知 ID。只读下载有独立断线/超时错误，不混用发送的不确定回执；仅明确不支持或文件 unavailable 可采用文档规定的 locator 刷新/安全回退，没有隐藏发送重试队列。

默认发送间隔 0.4 秒只是本地限速，不承诺满足 QQ 的风控规则。文件上传和正文分属不同 action，文件成功但说明文字失败会返回部分结果。超出 inline 大小的媒体使用共享路径或 NapCat 分块上传，接口不可用时返回明确错误。文本、媒体和上游动作均不能保证最终用户已读。

## 会话、工具与定时推送

插件注册 Hermes 的目标解析、校验、默认投递环境变量和 standalone sender。独立 sender 建立只发的正向连接，接收文本及 `(local_path, is_voice)` 媒体列表，复用 `prepare_media`/`send_prepared_media`。全部路径、大小和引用在首条消息前预检；`force_document` 覆盖分类。完成后关闭资源，反向独立 sender 明确拒绝。

发送工具属于 `napcat_qq`，getter、近期历史、`qq_get_forward` 和 `qq_read_media` 属于 `napcat_qq_read`。读开关不授予发送。handler 从 Hermes task-local session context 读取 platform/chat/user/profile/message，再解析该 profile 的实时 adapter。非 NapCat turn、无 Gateway 或无 adapter 均失败关闭。`qq_read_media` 下载在 Gateway loop，分析在调用者主机上下文执行；公共 registry handler 选择 native/aux vision，原生多模态结果保持原样。

工具目标默认当前会话。一般跨会话需要显式开关、管理员和目标白名单；媒体引用仍只能同会话使用。群消息验证群归属；私聊验证联系人/目标证据。读取输出有界文本、作者、时间、类型、有效媒体 ID，不返回 URL/原始事件。私聊历史逐条检查窗口/撤回，群历史沿用观察控制器。合并转发从已核验父消息取 ID，遍历共享深度/节点/字节预算，声称的节点作者不能授予权限。

模型提供的媒体 URL 先走 `MediaStore`，不会直接交给 NapCat 下载。本地路径仍受 `outbound_roots`、真实路径解析、工具字节上限和共享路径映射约束。合并转发先核验全部已有消息节点，再下载自建节点媒体，最后执行一次发送，避免验证中途产生不可逆动作。自建节点使用机器人 QQ 号，模型只能设置显示标签。

相同 session、当前消息和参数的并发发送工具调用在 adapter 的 Gateway loop 上共用一个 in-flight Task。Task 完成后立即移除，不形成长期幂等缓存。调用方取消后，已经调度的发送继续得到结果，避免上层把取消误判为“未执行”并自动重发。读取工具直接执行，取消会传播到 Gateway loop 的下载任务，清理未完成文件并释放缓存额度。媒体正文分开发送时，后半段失败会返回部分成功状态。

## 网络、媒体和存储

OneBot action 始终走 WS，入站附件默认也通过该认证连接下载。下载器验证 info/chunk/complete、规范 base64、块序/实际字节/完成总数，绑定 epoch；存储只在完整结束后发布 0600 UUID 文件。`auto` 仅按明确能力/locator 失败使用受控 HTTP；`stream` 不回退，`http` 是显式兼容模式。HTTP 两方向独立校验实际 DNS/重定向/origin，不转发 WS token、不读取入站本地路径。普通失效文件标识只从核验的原消息刷新一次。

缓存位于 Hermes profile 标准 document cache 的 `napcat` 子目录，并通过主机路径映射交给 sandbox 文件工具。只清理自身 UUID 过期常规文件，额度不足拒绝新下载；TTL 须覆盖任务时长。挂载/同步和跨进程总配额不由插件保证，不同实例应使用独立 profile。

本地、URL 和四种媒体引用共用共享/暂存/inline/stream 发送准备，下载及暂存共同计入额度。当前附件先占本轮预算，随后明确引用；近期仅在无当前/无引用时补入。失败下载消耗完整 allowance。引用在读取、事件构造、分析返回及发送锁内复核来源、期限和撤回。已有消息合并转发走 NapCat 服务端，无需材料化原媒体。

共享路径映射对应同一存储内容在 Hermes 和 NapCat 中的两条路径。管理员负责挂载，NapCat 只读。Windows 远端路径允许盘符路径；在 Linux 本机测试中只核验了路径字符串映射，未运行 Windows QQ。

## 生命周期和运维边界

forward start 等待账号验证，reverse start 等待监听成功。反向监听就绪与 QQ 连接就绪分开看待；CLI probe 会等待账号验证。forward 重连在传输层内部进行，Gateway 的静态连接状态不代表实时 QQ 登录状态，需要结合探测和日志。

停止时清理待响应 Future、worker、reader、HTTP session、反向监听和队列。可选群聊层还清理计时器、群请求任务、分类器连接及内存观察缓存。事件队列满会记录丢弃计数和日志；内存去重在进程退出后丢失。当前实现不引入数据库、RabbitMQ 或额外 Web 服务；可靠事件存档是后续扩展。

增大的 WS 上限配套入站队列总字节预算，额度包含正在处理的消息。撤回通知使用独立限量通道，不与媒体消息竞争字节额度。分块上传绑定连接 epoch，正式发送也在 WS 写锁内复核，防止重连期间使用旧进程路径。
