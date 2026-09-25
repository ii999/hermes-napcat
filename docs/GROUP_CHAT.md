# 群聊上下文与参与策略

本功能在 `feat/group-context-participation` 开发，默认关闭。它扩展现有 NapCat 适配器；私聊、OneBot 传输、发送结果不确定时不重试、Hermes 用户鉴权和原有 QQ 工具权限继续生效。Hermes 继续管理 Agent、会话和持久记录，插件不修改 Hermes 核心，不另建会话数据库。

## 启用上下文

把 `examples/group-chat.config.yaml` 合并到现有配置。保持 `NAPCAT_ALLOWED_USERS`、`allowed_groups` 和管理员配置。升级时必须同时更新 Python 包和目录插件入口：

```sh
uv run --no-project python scripts/install_plugin.py \
  --hermes-python /opt/hermes-agent/.venv/bin/python \
  --hermes-home /srv/hermes-home --upgrade
```

替换为自己的解释器与 profile 目录，随后重启 Gateway。只升级 wheel 可能留下旧 `plugin/tools.py`，导致无法发现新增工具。

```yaml
group_sessions_per_user: true
gateway:
  platforms:
    napcat:
      extra:
        group_context:
          enabled: true
          observe_untriggered: true
          observe_all_members: false
          history_backfill: true
```

配置位置是 `gateway.platforms.napcat.extra`；独立 CLI 的扁平配置直接使用 `group_context` 和 `proactive_assist`。

`observe_all_members: false` 只观察原有白名单用户及机器人自己的消息。管理员明确设置为 `true` 后，插件才把允许群里的其他成员发言作为背景。这不会允许这些成员触发 Agent、运行命令或调用工具。群成员应知晓观察范围、数据用途和模型提供方；不要为了观察群聊开启全局 `NAPCAT_ALLOW_ALL_USERS`，那会扩大执行及私聊权限。

## 群历史如何进入当前对话

在线期间，插件先保存有界的群级观察记录，再判断是否需要回复。普通聊天不调用主模型，不下载历史附件，也不消耗 Agent 调用额度。记录包括消息 ID、稳定 QQ 用户 ID、群名片/昵称、事件时间、@ 对象、引用关系、群角色及附件类型。群角色用于描述发言人，不赋予操作权限。附件只有占位符，机器人不能声称已经看过图片或听过语音。

在 `@机器人`、回复机器人或 `/ai` 触发时，插件把当前用户和原始请求放入正常 `MessageEvent`，把背景放入 Hermes 的 `channel_context`。插件保留 `source.user_id` 和机器人账号 scope，因此 `group_sessions_per_user: true` 的执行会话仍按用户隔离。公共群背景会在本轮向请求者的模型展示，个人私聊历史不会加入群背景。

冷启动、连接 epoch 变化、传输队列丢包、观察限流或关闭实时观察时，插件按需调用 `get_group_msg_history`。单次最多 `history_limit` 条，设置 `disable_get_url=true`、`parse_mult_msg=false`。它验证返回消息的群、作者、账号和时间，再与实时记录按消息 ID 去重。`message_id` 可为负数且不保证连续；代码不对它做递增或减法。分页使用来自已核验记录的锚点，遵循 NapCat 的短消息 ID 映射。

引用任意群友的消息再 @ 机器人时，插件从缓存或 `get_msg` 获取被引用文本，并优先保留它。引用必须属于同一获准群。超出常规时间窗的明确引用可以作为单独锚点进入当前上下文。插件不递归读取无限引用链；它也不会因为模型提供了一个数字就跨群读取消息。

默认不会在上一条机器人回复处截断，以免切断同群的其他话题。`stop_at_last_bot_message: true` 可启用该边界，且保留边界上的机器人消息。上下文是有界窗口，不能保证完整恢复离线期间的聊天；查询失败或过滤记录时，模型和读取工具会收到状态说明。插件不会在后台轮询全部群，也不会将历史回放成新的用户命令。

## 上下文配置

以下值均为默认值；`enabled` 是唯一的总开关。

```yaml
group_context:
  enabled: false
  observe_untriggered: true
  observe_all_members: false
  live_buffer_messages: 200
  max_groups: 64
  max_message_chars: 4000
  max_context_chars: 12000
  history_limit: 50
  history_window_seconds: 1800
  history_backfill: true
  backfill_timeout_seconds: 3
  backfill_cooldown_seconds: 30
  stop_at_last_bot_message: false
  max_pending_messages: 32
  observation_messages_per_minute: 120
```

缓存数量、单条文本、序列化上下文和待处理请求都有上限。时间均使用服务器时间；实时消息缺少有效时间时标记为接收时间，历史消息缺少有效时间则拒绝。字符预算包含 JSON 转义开销；超限时保留引用和较新的记录，并标记截断。稳定的 QQ ID 用于区分同名或改名成员，昵称以 JSON 字符串编码，换行无法伪造另一条结构化记录。

`observation_messages_per_minute` 按群和发言用户限制观察数量；原 `messages_per_minute` 继续限制 Agent 触发。历史/引用查询另有每群每分钟 30 次读预算。群白名单数量不得大于 `max_groups`。普通消息只更新观察缓存，不等待正在生成的回答；群请求队列与跨群处理并行度受配置限制。管理员控制命令可绕过等待模型的请求队列，但仍接受原有鉴权与限流。

## 主动参与与对话节奏

建议先观察，再开启 dry-run，最后用专用测试群允许实际主动回复。

```yaml
proactive_assist:
  enabled: true
  dry_run: true
  quiet_window_ms: 2200
  burst_window_seconds: 30
  confidence_threshold: 0.90
  cooldown_seconds: 120
  max_responses_per_hour: 4
  max_decisions_per_hour: 30
  max_reply_age_seconds: 90
  ignored_users: []
```

主动参与要求同时启用上下文及实时观察。同一成员在短时间内连续发送的片段会合并为候选问题；新成员接话会结束这个片段，新的群消息会重置静默计时。明确 @ 他人、引用他人或未知消息、命令、机器人自己的消息、忽略名单中的账号不会成为主动候选。QQ 没有在此实现中可依赖的通用 bot 身份标识，应把其他机器人 QQ 号配置到 `ignored_users`，避免互答。

未配置分类器时，插件只对“有人知道”“求助”“can anyone”等显式开放求助采用规则判断。规则决策的 1.0 是确定性标记，不是经过校准的语义置信度。它不提供完整话题理解，也不能保证区分玩笑、引用式求助或修辞问句。模型分类器启用后，可以评估更多疑问形式，并结合短期群背景决定是否插话。群聊处于快速互答时，新消息会使旧候选失效。

`dry_run: true` 只记录结构化决策类别，不调用主 Agent、不发送消息；启用外部分类型模型后，dry-run 仍会调用该分类器并产生其费用。冷却和次数预算在 dry-run 中也生效。默认日志不写群聊原文或模型自由文本。

`dry_run: false` 时，插件仅以获准发言人的真实身份发起普通会话，设置 `allow_gateway_control=False`，不伪造管理员或 internal 事件。**配置必须保持 `group_toolsets: []`**，以阻止主动会话执行 QQ 发图、文件写入、终端等副作用。需要群工具时，保留 dry-run 或关闭主动参与。自动背景读取不依赖模型工具，所以无工具模式仍可理解近期群聊。

插件在提交候选前检查群状态版本，并在发送第一段之前再次检查新消息与过期时间。过时主动回答不发送；第一段开始发送后，沿用原来的部分成功/不确定投递规则，避免取消已经开始的动作后再次发送。完整 Hermes 调度和任务上下文传播仍需实机验证。直接被叫到的请求不套用主动冷却，继续按原用户限流处理。

## 可选轻量模型分类器

分类器使用独立、无工具的 Chat Completions 请求；不为每条群消息启动完整 Hermes Agent。不配置时不增加外部模型依赖。

```yaml
proactive_assist:
  enabled: true
  dry_run: true
  classifier:
    enabled: true
    base_url: http://127.0.0.1:8000/v1
    model: your-small-instruct-model
    api_key_env: NAPCAT_CLASSIFIER_API_KEY
    timeout_seconds: 8
    allow_insecure_http: false
```

模型接口需支持 `response_format: {type: json_object}`。密钥从当前 Hermes profile 的 secret scope 读取，不在 YAML 中填写明文；密钥变量必须以 `NAPCAT_CLASSIFIER_` 开头，不能借用 OneBot token。模型 endpoint 只能由管理员配置，群消息无法修改。非 loopback 的明文 HTTP 需要显式允许；跨主机应使用 HTTPS 或受控隧道。

分类器请求不跟随重定向、不读取代理环境、不重试，响应限制 32 KiB。它必须返回布尔决定、0 到 1 的有限数值及准确的候选消息 ID。超时、错误 JSON、错误目标、超限响应均保持沉默，不自动退回更宽松规则。后台配置的 endpoint 是显式数据出口；不要把不可信群里的内容传给未获准的云服务。

## Agent 读取近期消息

新增 `qq_get_recent_messages`，注册到现有 `napcat_qq` 工具集。需要 `qq_tools.enabled: true`，并在目标会话开放该工具集。它默认查询当前群，只返回有界文本、发送者、时间、引用、附件类型和历史状态，不返回媒体 URL。

```json
{"limit": 30}
```

```json
{"limit": 20, "before_message_id": "已经读取到的群消息ID"}
```

`limit` 不得超过 `group_context.history_limit`。`before_message_id` 必须来自当前群的保留记录，分页仍受时间和返回量限制。跨群沿用 `allow_cross_chat`、管理员、目标群白名单三项条件，且仍检查发言用户的授权；不支持查询任意 QQ 号的历史。私聊调用时若没有显式获准群目标，会拒绝。

## 撤回、重启及数据边界

启用后，传输层会交付通过账号核验的 `group_recall` 通知。插件按群和消息 ID 移除缓存内容，保留有界 tombstone，取消待判断候选，并阻止同一窗口内的历史回填复活记录。已提交给模型、已写入 Hermes/provider 日志或已发出的消息无法通过这个缓存撤回操作收回。

观察缓存只在内存中，受时间窗和容量限制；关闭适配器会清空，进程重启依靠有界历史查询补充。插件没有持久事件存档、事务队列、完整离线补齐或跨进程精确去重保证。已触发请求的上下文还受 Hermes 与模型提供方各自的记录和保留规则管理，不能把内存 TTL 当成整个系统的删除承诺。

## 验收

测试覆盖记录归属/时间/字节边界、实时与历史去重、观察权限与执行权限分离、引用核验、撤回、请求队列、冷却/预算、过时发送检查和本地 HTTP 分类器协议。适配器测试使用仓库的 Hermes doubles，不能代替实际 Hermes + QQ 联调。

实机验收应包含：其他群友讨论后突然 @、同名或改名成员、引用较旧消息、用户连发短句、真人抢先回答、分类器故障、断线后历史缺口、生成期间撤回及新消息、非管理员控制命令、其他机器人互答，以及正反向连接两种模式。开启实际主动回复前，先在 dry-run 中核对决策与群成员预期。

接口核验来源：Hermes `v2026.9.14` 的 `gateway/platforms/event.py`（`channel_context`、引用作者与控制权限字段），NapCatQQ `0b4cfe65ed889aa8e8061ee4c5b7edddec0d10f5` 的 `packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts`。本次未运行真实 QQ 登录、模型推理或完整 Hermes Gateway。
