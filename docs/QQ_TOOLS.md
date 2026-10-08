# Agent QQ 工具

工具使用当前 profile 的实时 Gateway/NapCat 连接，读取注册到 `napcat_qq_read`，发送注册到 `napcat_qq`。不建立第二条连接，不接受任意 OneBot action。

## 工具清单

| 工具 | 工具集 | 用途 |
| --- | --- | --- |
| `qq_send_message` | `napcat_qq` | 文本、图片、@、表情及引用，支持图文交错 |
| `qq_send_media` | `napcat_qq` | 单个图片、语音、视频或文件，可附说明和视频封面 |
| `qq_send_forward` | `napcat_qq` | 合并转发卡片，自建多媒体节点或已核验消息引用 |
| `qq_get_message` | `napcat_qq_read` | 已核验消息的文字、作者、附件类型及有效引用 |
| `qq_get_media` | `napcat_qq_read` | 同会话引用的受控缓存路径/MIME/大小和 `source` |
| `qq_read_media` | `napcat_qq_read` | 通过 Hermes 已安装 handler 理解图片、语音、视频或文档 |
| `qq_get_chat_info` | `napcat_qq_read` | 当前或获准目标会话的名称和类型 |
| `qq_get_recent_messages` | `napcat_qq_read` | 有界私聊/群历史；群查询需要 `group_context` |
| `qq_get_forward` | `napcat_qq_read` | 从已核验父消息展开有界合并转发，返回文本及引用 |

这些模型工具只能在 NapCat 触发的实时 Gateway turn 中调用。CLI、TUI、其他平台会话及独立 cron 没有当前 QQ 身份，会拒绝执行。Hermes 的主机投递与独立正向 cron 文本/本地媒体接口按各自规则工作。

## 启用

升级目录插件入口和 Python 包：

```sh
uv run --no-project python scripts/install_plugin.py \
  --hermes-python /opt/hermes-agent/.venv/bin/python \
  --hermes-home /srv/hermes-home \
  --upgrade
```

将以下配置合并到 Hermes `config.yaml`。私聊通过 `platform_toolsets.napcat` 获得工具；群聊使用适配器的 `group_toolsets` 覆盖值。

```yaml
platform_toolsets:
  napcat: [napcat_qq_read]

gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        group_toolsets: [napcat_qq_read]
        qq_tools:
          enabled: false
          read_enabled: true
          allow_cross_chat: false
        media:
          references:
            enabled: true
          outbound_roots:
            - /srv/qq-output
```

这是只读配置。需要发送时另加 `napcat_qq` 并设置 `qq_tools.enabled: true`。`read_enabled: true` 只允许读取；旧 `enabled: true` 允许两类调用，但 Hermes 仍必须授予对应工具集。升级前仅使用 `napcat_qq` 的会话要显式增加 `napcat_qq_read` 才能发现原来的 getter。Python 包与目录入口必须同时更新。

不允许群成员调用模型工具时保持 `group_toolsets: []`，私聊授权不自动授予群聊。

实际主动参与要求 `group_toolsets: []`，因此开启群工具时必须关闭 `proactive_assist` 或保持 `dry_run: true`。自动群背景注入不依赖模型调用工具，无工具模式仍可在被叫到时使用近期历史。

## 读取私聊和群历史

`qq_get_recent_messages` 默认读取当前会话，不返回媒体 URL。私聊调用 `get_friend_msg_history`，受 `qq_tools.history_limit`（默认 50）和 `history_window_seconds`（默认 1800）限制，逐条检查联系人、账号、作者、时间和撤回。机器人作者本身不能证明私聊目标。群查询需要 `group_context.enabled: true`，沿用其历史窗口和读预算。

```json
{"limit": 30}
```

`limit` 不得超过相应历史配置。`before_message_id` 必须来自同会话已核验消息；群锚点还须在保留记录内，文件通知的派生 ID 不能作分页锚点。结果可包含四种媒体的引用、过滤/截断状态；不保证完整离线历史。跨会话仍检查管理员及目标 ACL。群背景配置见 [GROUP_CHAT](GROUP_CHAT.md)。

## 读取合并转发

```json
{"message_id":"包含合并转发的当前会话消息ID"}
```

这是 `qq_get_forward` 的参数，不能传任意 `forward_id`。插件先验证父消息同账号、同会话、作者可见、未撤回且在窗口内，再从其真实 forward 段取 ID。默认最多 3 层、50 个节点、50,000 UTF-8 文本字节，共享遍历预算并检测循环。节点作者是卡片内声称的 attribution，不成为授权主体；媒体权限和 TTL 绑定已核验父消息。结果中的 `truncated`、`unavailable`、`media_refs_truncated` 表示不完整展开，不是完整档案。

## 目标与权限

所有工具默认操作发起当前 turn 的 QQ 会话。`target` 留空即可。显式目标使用：

```text
private:123456789
group:987654321
```

跨会话操作必须同时满足：

- `qq_tools.allow_cross_chat: true`
- 当前发言用户位于 `admins`
- 私聊目标通过用户白名单，或群目标位于 `allowed_groups`

工具无法跨 profile 借用另一账号的 adapter。引用、消息读取及已有消息转发都核验来源：群消息须属于目标群；私聊须来自目标联系人且无冲突 destination，或是机器人发给该联系人的消息。机器人消息缺 destination 时还须由本进程按目标记住。当前入站锚点不会覆盖冲突账号、作者或目标证据，无法证明来源就拒绝。

## 多媒体来源

`source` 接受允许目录内的绝对本地路径、通过 `media.outbound` 策略检查的 HTTP(S) URL，显式 `base64://` / `data:<mime>;base64,` 输入，以及同会话 `media:<id>` 媒体引用。引用类型须匹配发送类型；语音引用的 `record` 对应 `qq_send_media` 的 `audio`。插件先在 Hermes 侧检查媒体，不把任意 URL 直接交给 NapCat 下载。

已映射的本地文件直接用共享路径；其他已检查媒体可通过 `shared_cache_dir` 复制到专用共享暂存目录，未配置时使用受限 base64，超限时尝试 NapCat 分块上传。共享映射本身不提供挂载或跨主机同步。基础文档发送也支持该传输选择。出站公网模式、媒体大小与 WS 预算、共享暂存/配额的完整配置见 [MEDIA](MEDIA.md)。

本地图片受 `media.max_bytes` 和文件头检查约束，同时受 `qq_tools.max_local_media_bytes`（默认 256 MiB）限制；其他本地媒体沿用工具大小上限。没有共享目录时，超过实际 inline/WS 预算的文件使用受限分块上传；关闭 streaming 后则拒绝超限文件。

启用 `media.references.enabled` 后，当前附件、消息/历史/合并转发读取或群上下文可提供媒体标识。`qq_get_media` 参数为 `{"media_id":"qqmedia_实际标识"}`，返回 Agent 可见的 `path`、MIME、字节数和 `source`，不自动理解内容。自动引用/近期补入使用正常 Hermes 媒体事件，详见 [媒体指南](MEDIA.md)。

需要理解时调用：

```json
{"media_id":"qqmedia_实际标识","question":"附件说明了什么？"}
```

`qq_read_media` 通过 Hermes 注册的 `vision_analyze`（原生多模态或辅助视觉）、`video_analyze`、公共转录接口或 `read_file` 处理内容。语音尊重 runner STT 开关，只尝试已安装的本地 fallback。文档可传 `offset` 和 `limit`，默认 1/200、最多 500 行；普通字符串结果最多 16,000 字符，截断显式标记。缺失 handler、模型或依赖会明确失败。图片原生多模态结果保持 Hermes 原始 envelope，不转成普通 JSON 字符串。

原样发回示例：

```json
{"media_type":"image","source":"media:qqimg_实际标识"}
```

四种媒体引用都绑定当前账号与会话，有期限和撤回检查；管理员也不能跨聊天使用引用。缓存路径不因此加入 `outbound_roots`。重启、到期或淘汰后引用不可用；locator 恢复规则不延长期限。

图片说明与图片放在同一条消息。QQ 客户端对语音、视频和文件混合正文的表现不一致，因此插件先发送媒体，再单独发送说明文字。媒体成功而说明失败时，工具返回 `partial: true` 和已知消息 ID；Agent 不应自动重试整个动作。

流式入站语音请求 NapCat 转换为 MP3，出站不提供通用音视频转码。真实编码、视觉和 STT 能力须在部署环境验证。

## 图文消息

只发文本或随后附图时可使用简写参数：

```json
{
  "text": "本轮测试结果",
  "images": ["/srv/qq-output/result.png"]
}
```

需要图文交错时使用 `segments`：

```json
{
  "segments": [
    {"type": "text", "text": "图一："},
    {"type": "image", "source": "/srv/qq-output/a.png"},
    {"type": "text", "text": "图二："},
    {"type": "image", "source": "/srv/qq-output/b.png"}
  ],
  "reply_to": "123456"
}
```

图文内容在发送前按整条 UTF-8 JSON / WS 预算预检，超限时按消息段保序拆分；引用只在第一条。任意单段超限会在首条发送前拒绝。单条成功保留 `message_id`，多条成功还返回 `message_ids`。中途失败返回已知 ID、`partial` 和 `delivery_uncertain`，不得重试整批。

直接消息允许 `text`、`image`、`at` 和 `face` 段。插件拒绝 `@all`，也不会解析文本中的 CQ code。

## 合并转发

`qq_send_forward` 允许两种节点混合出现。卡片保持单次动作，不自动拆卡；请求总大小超限时应采用共享目录或减少节点。

已有消息节点：

```json
{"message_id": "123456"}
```

这类节点先核验同会话来源，再由 NapCat 服务端合并转发，可避免原媒体下载/重新上传。不开放原始单条转发 action；其受理但没有 message ID 的结果不能当作普通已完成发送。

自建节点：

```json
{
  "label": "分析助手",
  "segments": [
    {"type": "text", "text": "结论与图表"},
    {"type": "image", "source": "/srv/qq-output/chart.png"},
    {"type": "file", "source": "/srv/qq-output/report.pdf", "name": "完整报告.pdf"}
  ]
}
```

完整调用示例：

```json
{
  "nodes": [
    {"message_id": "123456"},
    {"label": "分析助手", "text": "以下是整理后的结论。"},
    {
      "label": "分析助手",
      "segments": [
        {"type": "image", "source": "/srv/qq-output/chart.png"},
        {"type": "file", "source": "/srv/qq-output/report.pdf", "name": "报告.pdf"}
      ]
    }
  ],
  "source": "分析报告",
  "summary": "共 3 条",
  "prompt": "查看完整内容",
  "preview": ["原始问题", "结论", "附件"]
}
```

自建节点固定使用机器人 QQ 号作为 `user_id`，`label` 只改变显示昵称。插件把 `source`、`summary`、`prompt` 和 `preview` 传给 NapCat；具体卡片样式受 NapCat packet mode 与 QQ 客户端版本影响。

## 限制参数

可在 `qq_tools` 下调整：

```yaml
qq_tools:
  enabled: true
  read_enabled: true
  allow_cross_chat: false
  max_segments: 64
  max_media_items: 8
  max_forward_nodes: 50
  max_forward_chars: 50000
  max_forward_depth: 3
  history_limit: 50
  history_window_seconds: 1800
  max_local_media_bytes: 268435456
```

`max_forward_chars` 在转发读取中计 UTF-8 文本字节，在自建发送节点中计文本字符；`max_forward_depth` 限制读取遍历。私聊历史使用这里的数量/时间窗，群历史使用 `group_context` 对应字段。

相同 session、当前消息和参数的并发调用会共用同一个正在执行的 action，避免并行工具调度产生重复发送。操作完成后不保留幂等缓存，后续相同请求仍可再次发送。写入后超时或断线会返回 `delivery_uncertain: true`，此时应先检查 QQ 会话。

base64 输入的长度、类型和整批预算，以及分块上传配置见 [STREAM_UPLOAD](STREAM_UPLOAD.md)。
