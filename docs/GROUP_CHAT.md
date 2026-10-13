# 群聊上下文与参与策略

群聊推荐对指定群开放全员对话、共享群会话，并开启背景观察和受邀续聊，未被叫到时的主动插话保持关闭。群聊配置示例已采用这组设置；省略扩展配置时仍保持关闭，续聊默认使用 Hermes 当前 Agent，无需新增模型接口。它扩展现有 NapCat 适配器；私聊、OneBot 传输、发送结果不确定时不重试、Hermes 用户鉴权和原有 QQ 工具权限继续生效。Hermes 继续管理 Agent、会话和持久记录，插件不修改 Hermes 核心，不另建会话数据库。

## 启用上下文

替换 `examples/group-chat.config.yaml` 中的示例群号，再合并到现有配置，沿用 Hermes 当前模型。示例推荐开启实际续聊，设置了 `continuation.enabled: true` 和 `dry_run: false`。保持 `NAPCAT_ALLOW_ALL_USERS=false`，保留私聊用户和管理员的 `NAPCAT_ALLOWED_USERS` 配置，并保留其他需要的 `allowed_groups` 项。升级时必须同时更新 Python 包和目录插件入口：

```sh
uv run --no-project python scripts/install_plugin.py \
  --hermes-python /opt/hermes-agent/.venv/bin/python \
  --hermes-home /srv/hermes-home --upgrade
```

替换为自己的解释器与 profile 目录，随后重启 Gateway。只升级 wheel 可能留下旧 `plugin/tools.py`，导致无法发现新增工具。

```yaml
group_sessions_per_user: false
gateway:
  platforms:
    napcat:
      extra:
        allowed_groups: ["987654321"]
        group_allowed_chats: ["group:987654321"]
        group_context:
          enabled: true
          observe_untriggered: true
          observe_all_members: true
          history_backfill: true
```

配置位置是 `gateway.platforms.napcat.extra`；独立 CLI 的扁平配置直接使用 `group_context` 和 `proactive_assist`。

`group_allowed_chats` 是独立的群成员授权列表，格式为 `group:<群号>`，其中每个群也必须在 `allowed_groups` 中。列出的群允许所有成员触发 Agent，并可在受邀窗口内接话；未列出的允许群仍检查用户白名单。默认列表为空，不接受 `*`、私聊目标或未被允许的群。配置通过 Hermes 原生的群会话授权检查，不伪造管理员身份，也不修改全局环境。授权配置变更后重启 Gateway。

`NAPCAT_ALLOWED_USERS` 继续限制私聊；`NAPCAT_ALLOW_ALL_USERS` 应保持 `false`，开启它仍会全局放行。群全员授权不授予管理员权限：只有 `admins` 中明确配置且在用户白名单内的成员才能使用 Gateway 控制操作，QQ 群主或管理员标签不构成授权。群工具集仍默认为空；若另行启用工具，工具权限和跨会话管理员限制照常生效。

`observe_all_members: false` 观察获准在该群对话的成员及机器人自己的消息，因此全员授权群也会观察所有成员。`true` 还会把其他允许群中的未授权成员发言作为背景，但不会让这些成员触发 Agent。推荐配置显式开启全员观察。群成员应知晓观察范围、共享会话、数据用途和模型提供方；不要为了群聊开放全局 `NAPCAT_ALLOW_ALL_USERS`。

## 群历史如何进入当前对话

在线期间，插件先保存有界的群级观察记录，再判断是否需要回复。未进入受邀续聊或主动参与判断的普通聊天不调用主模型，观察本身不下载历史附件。记录包括消息、作者、时间、@、引用、角色及附件类型，角色不赋予权限。开启短期媒体引用后可附带 `media_id`，但元数据不代表理解内容。群文件通知只按观察策略保留，不单独触发 Agent。

在 `@机器人`、回复机器人或 `/ai` 触发时，插件把当前用户和原始请求放入正常 `MessageEvent`，把背景放入 Hermes 的 `channel_context`。插件保留 `source.user_id` 和机器人账号 scope；推荐 `group_sessions_per_user: false`，让同一群成员共享 Hermes 群会话，各条消息仍保留真实作者。此项是 Hermes 顶层设置，也会影响其他平台的群会话。保持 `true` 时执行会话按用户隔离，但各成员仍可使用公共群背景并跨成员续聊。共享群会话不会把个人私聊历史加入群背景。

冷启动、连接 epoch 变化、传输队列丢包、观察限流或关闭实时观察时，插件按需调用 `get_group_msg_history`。单次最多 `history_limit` 条，设置 `disable_get_url=true`、`parse_mult_msg=false`。它验证返回消息的群、作者、账号和时间，再与实时记录按消息 ID 去重。`message_id` 可为负数且不保证连续；代码不对它做递增或减法。分页使用来自已核验记录的锚点，遵循 NapCat 的短消息 ID 映射。

引用任意群友的消息再 @ 机器人时，插件从缓存或 `get_msg` 获取被引用文本，并优先保留它。引用必须属于同一获准群。超出常规时间窗的明确引用可以作为单独锚点进入当前上下文。插件不递归读取无限引用链；它也不会因为模型提供了一个数字就跨群读取消息。

默认不会在上一条机器人回复处截断，以免切断同群的其他话题。`stop_at_last_bot_message: true` 可启用该边界，且保留边界上的机器人消息。上下文是有界窗口，不能保证完整恢复离线期间的聊天；查询失败或过滤记录时，模型和读取工具会收到状态说明。插件不会在后台轮询全部群，也不会将历史回放成新的用户命令。

## 可选历史媒体

`media.references.enabled: true` 为允许观察的图片、语音、视频和文件保留短期同会话 `media_id`。当前附件先占预算，`attach_quoted` 随后补入明确引用，两者共享默认 4 个/64 MiB 上限。只有无当前附件且无明确引用时，`attach_recent` 才补入同发言人的近期附件。读取其他作者附件仍检查观察范围。

群观察/回填只保存元数据，`disable_get_url=true` 保持不变；读取时优先认证流式下载，HTTP 仅按媒体策略兼容。过期/撤回或 locator 不可用时不保证恢复。主动参与不自动下载附件，实际主动参与仍要求空工具集。

自动附件补入不要求模型工具。`qq_get_media`/`qq_read_media` 需要 `napcat_qq_read` 和读开关，`media:<id>` 原样发送另需 `napcat_qq` 和发送开关。路径返回不等于内容理解；Hermes handler 决定视觉/转录/视频/文档能力。见 [MEDIA](MEDIA.md)。

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

`observation_messages_per_minute` 按群和发言用户限制观察数量；原 `messages_per_minute` 继续限制 Agent 触发。历史/引用查询另有每群每分钟 30 次读预算。群白名单数量不得大于 `max_groups`。普通消息只更新观察缓存，不等待正在生成的回答；群请求队列与跨群处理并行度受配置限制，处理名额保留到 Hermes 后台任务结束。管理员控制命令及其待处理澄清问题的回答可绕过等待队列，但仍接受原有鉴权、触发条件与限流。

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

对于未受邀的主动插话，`dry_run: true` 只记录结构化决策类别，不调用主 Agent、不发送消息；启用独立分类器后，dry-run 仍会调用该分类器并产生其费用。Hermes 原生续聊的 dry-run 会调用主 Agent，见下节。冷却和次数预算在 dry-run 中也生效。默认日志不写群聊原文或模型自由文本。

`dry_run: false` 时，插件仅以获准发言人的真实身份发起普通会话，设置 `allow_gateway_control=False`，不伪造管理员或 internal 事件。**配置必须保持 `group_toolsets: []`**，以阻止主动会话执行 QQ 发图、文件写入、终端等副作用。需要群工具时，关闭 Hermes 原生续聊，并保留主动插话 dry-run 或关闭主动参与。自动背景读取不依赖模型工具，所以无工具模式仍可理解近期群聊。

插件在提交候选前检查群状态版本，并在发送第一段之前再次检查新消息与过期时间。过时主动回答不发送；第一段开始发送后，沿用原来的部分成功/不确定投递规则，避免取消已经开始的动作后再次发送。完整 Hermes 调度和任务上下文传播仍需实机验证。直接被叫到的请求不套用主动冷却，继续按原用户限流处理。

## 被叫到后的连续对话

`proactive_assist.continuation` 让机器人在被 @、用 `/ai` 叫到或被明确回复后，继续参与同一段群聊。窗口内的短答、补充、纠正和陈述句都可以交给 Hermes Agent 判断，不需要每次再 @。每条后续消息仍须通过当前群的授权规则，使用该条消息真实发言人的身份；一次邀请不会扩大其他群或私聊权限。全员授权群中，A 唤醒后，B、C 的相关发言也可以由 Hermes 判断并回复，不局限于最初触发者。

下面是推荐的群聊配置，合并到 `gateway.platforms.napcat.extra`。默认推荐开启实际续聊，`proactive_assist.enabled: false` 关闭未被邀请时的主动插话。需要观察决策而暂不发送续聊回复时，可将 `dry_run` 改为 `true`；判断仍会接收上下文并产生模型调用费用。

```yaml
allowed_groups: ["987654321"]
group_allowed_chats: ["group:987654321"]
group_toolsets: []
group_context:
  enabled: true
  observe_untriggered: true
proactive_assist:
  enabled: false
  dry_run: false
  quiet_window_ms: 2200
  confidence_threshold: 0.90
  continuation:
    enabled: true
    idle_seconds: 120
    max_duration_seconds: 600
    max_responses_per_hour: 30
    max_decisions_per_hour: 120
  classifier:
    enabled: false
```

默认 `classifier.enabled: false` 时，插件通过 Hermes 已注册的消息处理器发起一次普通 Agent 调用，沿用当前模型、认证、会话和工具集限制。同一次调用结合邀请消息和近期群聊决定是否接话，并生成需要发送的正文；不另建 Agent，也不需要分类器 endpoint、model 或密钥。

Agent 必须返回包含准确 `target_message_id`、`action` 和 `text` 的 JSON 对象。`reply` 携带非空正文；`listen` 表示相关话题暂不需要回复；`end` 关闭话题；`unrelated` 不延长窗口。后三者正文必须为空。字段、目标、长度或动作不合法时保持沉默，不把原始输出作为回复。模型的实际判断质量和格式遵循能力仍需实测。

原生续聊包括 dry-run 都要求 `group_toolsets: []`。判断期间的进度、流式内容、错误提示及附件投递被发送门禁拦截，媒体在读取或上传前被拒绝。通过校验的最终正文由插件作为纯文本发送，不交给 Hermes 的附件指令解析或 TTS。结构化结果可能保留在 Hermes 会话记录中；插件不修改或删除主 Agent 的持久记录。即使决定旁听、不相关或 dry-run，这条路径也会消耗一次主 Agent 调用及用户限流额度。

需要降低主模型判断成本时，可以显式配置独立分类器（见下节）。此时先由分类器返回是否响应和 `conversation_status`，仅获准回复后调用 Hermes。`continue` 可回复或旁听；`end`、`unrelated` 必须同时为 `respond: false`。缺少状态、状态矛盾、错误目标 ID、超时或低置信度不会触发续聊或延长窗口，也不会自动回退到主 Agent 判断。

相关对话按消息在本地的接收时间续期；机器人自己的消息、无关闲聊、未授权发言和模型调用耗时不会续期。默认连续 120 秒没有获判为相关的新内容就停止；即使不断续期，同一次邀请也最多持续 600 秒。明确判断话题结束会立即关闭窗口，下一次被叫到才能重新开启。窗口过期按单调时钟检查，不发送“已冷却”提示；重启或断开适配器会清空状态，历史回填不能重新开启窗口。邀请消息被撤回或离开保留缓存也会关闭窗口。

续聊有独立的每小时判断/回复预算，不受主动插话的 `cooldown_seconds` 限制，仍受原有用户限流、静默等待和跨群处理名额限制。同一成员连续发出的片段合并判断；生成回答时收到的补充消息会保留一个有界候选，等当前回答完成后结合新回答重新判断。新的真人发言或撤回会使旧候选失效，机器人发送及回显不会丢弃等待中的补充。@ 他人、引用他人或未知消息、命令和 `ignored_users` 中的账号不能触发续聊。控制命令会关闭续聊窗口，其他明确命令也不会开启窗口。

实际续聊沿用主动参与的保护：`group_toolsets: []`、`allow_gateway_control=False`、不自动下载附件，发送第一段前复核上下文版本、邀请窗口和回复有效期。已开始发送后仍遵循部分成功/不确定投递规则，不自动重发。开启 `dry_run` 后，直接被叫到仍正常处理，后续自动判断不发送正文；默认仍调用 Hermes，启用独立分类器时则只调用分类器。窗口和预算照常变化。

如果同时开启 `proactive_assist.enabled: true`，续聊关闭后，独立的主动插话规则仍可能响应开放求助。要保证冷却后一直等下一次被叫到，保持该开关为 `false`。配置修改后需重启 Gateway。

## 可选轻量模型分类器

分类器默认关闭。开启后使用独立、无工具的 Chat Completions 请求作为前置筛选，只有决定回复才调用 Hermes；适合另有轻量模型的部署。未开启续聊时，省略分类器的主动插话只使用上述规则。

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

`qq_get_recent_messages` 注册到 `napcat_qq_read`，需要该工具集和 `qq_tools.read_enabled: true`（或旧 `enabled: true`）。群查询需 `group_context`，返回有界文字、作者、时间、引用、媒体类型/引用和历史状态，不返回 URL。私聊同名工具使用独立有界联系人历史，配置见 [QQ_TOOLS](QQ_TOOLS.md)。

```json
{"limit": 30}
```

```json
{"limit": 20, "before_message_id": "已经读取到的群消息ID"}
```

群 `limit` 不得超过 `group_context.history_limit`，锚点须来自本群保留记录；分页受窗口和读预算限制。跨会话仍须开关、管理员和目标 ACL。私聊查询只返回核验属于获准联系人的记录，不能凭任意 QQ ID 读取历史。`qq_get_forward` 另从已核验父消息有界展开，卡片内作者名不赋予权限。

## 撤回、重启及数据边界

启用后，传输层会交付通过账号核验的 `group_recall` 通知。插件按群和消息 ID 移除缓存内容，保留有界 tombstone，取消待判断候选，并阻止同一窗口内的历史回填复活记录。已提交给模型、已写入 Hermes/provider 日志或已发出的消息无法通过这个缓存撤回操作收回。

观察缓存只在内存中，受时间窗和容量限制；关闭适配器会清空，进程重启依靠有界历史查询补充。插件没有持久事件存档、事务队列、完整离线补齐或跨进程精确去重保证。已触发请求的上下文还受 Hermes 与模型提供方各自的记录和保留规则管理，不能把内存 TTL 当成整个系统的删除承诺。

## 验收

测试覆盖群全员授权与私聊隔离、跨群拒绝、真实发言人和管理员限制、群成员上下文/媒体/工具访问、记录归属/时间/字节边界、实时与历史去重、观察权限与执行权限分离、引用核验、撤回、请求队列、冷却/预算、过时发送检查和本地 HTTP 分类器协议。适配器测试使用仓库的 Hermes doubles，不能代替实际 Hermes + QQ 联调。

续聊测试另外覆盖：只在受邀窗口内接收普通文字、保持当前发言人身份、相关旁听续期、话题结束、无关内容不续期、最长时限、重新唤醒、生成期间补充、独立预算、dry-run、分类器状态校验和发送前过期拦截。另有 Hermes handler 模拟测试覆盖结构化结果、纯文本投递、旁听/结束、无效输出拒绝、进度和延迟子任务拦截、跨目标发送、媒体预处理拦截、预算、dry-run 和不确定投递不重试。这些是模拟接口和本地 HTTP 协议验证，不代表真实模型已能准确判断群话题。

实机验收应包含：其他群友讨论后突然 @、同名或改名成员、引用较旧消息、用户连发短句、真人抢先回答、分类器故障、断线后历史缺口、生成期间撤回及新消息、非管理员控制命令、其他机器人互答，以及正反向连接两种模式。开启实际主动回复前，先在 dry-run 中核对决策与群成员预期。

接口核验来源：Hermes `v2026.9.14` 的 `gateway/platforms/event.py`（`channel_context`、引用作者与控制权限字段），NapCatQQ `0b4cfe65ed889aa8e8061ee4c5b7edddec0d10f5` 的 `packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts`。本次未运行真实 QQ 登录、模型推理或完整 Hermes Gateway。
