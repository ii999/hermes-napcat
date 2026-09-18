# 实现架构

## 模块职责

`config.py` 校验配置并隐藏错误输入中的密钥；`protocol.py` 负责 OneBot 消息段、目标和标识符；`policy.py` 负责白名单、触发、限流和有界去重；`transport.py` 负责正反向 WebSocket、账号校验和 action/echo；`media.py` 负责下载与受控路径映射；`adapter.py` 构造 Hermes 事件并执行受控 QQ 动作；`tools.py` 绑定当前 session、校验工具参数并注册 `napcat_qq`；`plugin.py` 注册平台和主机驱动投递钩子；`cli.py` 提供人工诊断。

目录插件 `plugin/__init__.py` 暴露 `register(ctx)`，`plugin/tools.py` 为 Hermes 的延迟平台加载器提供独立 `register_tools(ctx)`。实现代码装入 Hermes 的 Python 环境。工具发现不会导入 adapter 或连接 QQ；平台加载时重复注册采用 Hermes 的同一插件作用域。

## 入站与鉴权

OneBot reader 先区分事件和 API 响应。建立连接后调用 `get_login_info`，核对预期机器人账号。账号确认前只临时缓存有界消息，错误账号不进入事件处理。响应在 reader 内按 echo 完成 Future，事件进入单独 worker，因而事件处理中调用 `get_msg` 等 API 不会阻塞响应 reader。

worker 按聊天键顺序进入 adapter，跨聊天最多并行 event_workers 个。插件先检查账号、用户、群；再处理去重、限流和触发。群回复触发仅接受本进程记得的机器人消息，或通过 `get_msg` 验证同一群、机器人作者的消息。模型或用户提供的 quote sender 字段不能作为授权依据。

插件随后处理允许的媒体并调用 `build_source()`，保留用户身份、聊天类型、消息 ID 和账号 scope 元数据，通过 `handle_message()` 交给 Hermes。用户/群 ID 不复用同一裸数字地址。实际会话键、profile 路由、memory、模型调用及 cron 调度由 Hermes 负责。多个机器人账号应分配独立 profile；不依赖未验证的跨账号 session-key 推断。

网关鉴权仍然启用。插件不会设置 `internal=True` 或伪造 `role_authorized=True`。非管理员事件设置 `allow_gateway_control=False`。群聊工具集默认用空列表覆盖；私聊使用 Hermes 平台工具配置。

## 发送语义

发送目标先经 `private:<QQ>` / `group:<ID>` 解析和 ACL 校验，模型回复作为纯 text 消息段。引用使用独立 reply 段。分段过程保持文本内容并限制每段长度，只有第一段带引用。每次 OneBot action 使用新 echo；成功必须收到 `status=ok` 且整数 `retcode=0`。

`status=async` 是受理，不表示完成。连接断开、写后超时或缺失 message_id 表示结果不确定，禁止自动重发。多段发送在中途失败后返回已知成功消息 ID。读操作和写操作采用同一种保守失败策略，没有隐藏重试队列。

默认发送间隔 0.4 秒只是本地限速，不承诺满足 QQ 的风控规则。文件上传和正文分属不同 action，文件成功但说明文字失败会返回部分结果。超出 inline 大小的媒体须配置共享路径。文本、媒体和上游动作均不能保证最终用户已读。

## 会话、工具与定时推送

插件注册 Hermes 的 `parse_target_ref_fn`、`validate_target_ref_fn`、`cron_deliver_env_var` 和 `standalone_sender_fn`，由主机驱动发送。独立 sender 建立只发不处理事件的正向连接，结束后关闭资源。反向模式需要已经运行的 Gateway，独立 sender 返回明确错误。

`napcat_qq` 只暴露五个有界工具，不提供任意 OneBot action 透传。handler 从 Hermes `gateway.session_context` 读取当前 platform、chat、user、profile 和 message ID，再从正在运行的 Gateway 解析该 profile 自己的 adapter。非 NapCat turn、无实时 Gateway 或 profile 没有 adapter 时均失败关闭。

工具目标默认固定为当前会话。跨会话需要配置显式开启、当前用户属于 `admins`、目标通过 adapter 白名单三项条件。群消息引用通过 `group_id` 核验；私聊引用只接受目标联系人发来的消息、当前入站消息，或本进程按目标记住的机器人消息。读取工具只返回有界文本、发送者和附件类型，不返回媒体 URL 或原始事件。

模型提供的媒体 URL 先走 `MediaStore`，不会直接交给 NapCat 下载。本地路径仍受 `outbound_roots`、真实路径解析、工具字节上限和共享路径映射约束。合并转发先核验全部已有消息节点，再下载自建节点媒体，最后执行一次发送，避免验证中途产生不可逆动作。自建节点使用机器人 QQ 号，模型只能设置显示标签。

相同 session、当前消息和参数的并发工具调用在 adapter 的 Gateway loop 上共用一个 in-flight Task。Task 完成后立即移除，不形成长期幂等缓存。调用方取消后，已经调度的发送继续得到结果，避免上层把取消误判为“未执行”并自动重发。媒体正文分开发送时，后半段失败会返回部分成功状态。

## 网络、媒体和存储

媒体 HTTP 下载与 OneBot API HTTP 是不同链路：OneBot 的收发始终走 WS；必要的附件字节通过受控 HTTP(s) 下载。下载时不转发 WS token，不使用网络代理环境变量，每跳校验地址和实际解析 IP。默认不读取入站本地路径。

缓存只删除符合插件 UUID 命名规则且过期的常规文件，保留其他文件。额度不足时拒绝新下载，不删除新近附件；缓存 TTL 必须大于预计 Agent 任务时长。缓存不提供跨进程严格总配额，因此不同实例应使用不同 profile 目录。

共享路径映射对应同一存储内容在 Hermes 和 NapCat 中的两条路径。管理员负责挂载，NapCat 只读。Windows 远端路径允许盘符路径；在 Linux 本机测试中只核验了路径字符串映射，未运行 Windows QQ。

## 生命周期和运维边界

forward start 等待账号验证，reverse start 等待监听成功。反向监听就绪与 QQ 连接就绪分开看待；CLI probe 会等待账号验证。forward 重连在传输层内部进行，Gateway 的静态连接状态不代表实时 QQ 登录状态，需要结合探测和日志。

停止时清理待响应 Future、worker、reader、HTTP session、反向监听和队列。事件队列满会记录丢弃计数和日志；内存去重在进程退出后丢失。首版不引入数据库、RabbitMQ 或额外 Web 服务，降低部署复杂度；可靠事件存档是后续扩展。
