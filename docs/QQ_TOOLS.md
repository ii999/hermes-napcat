# Agent QQ 工具

`napcat_qq` 工具集让 Hermes Agent 使用当前 Gateway 的 NapCat WebSocket 连接操作 QQ。工具不建立第二条连接，也不接受任意 OneBot action 名称。

## 工具清单

| 工具 | 用途 |
| --- | --- |
| `qq_send_message` | 发送文本、图片、@、QQ 表情及引用回复，支持多图和图文交错 |
| `qq_send_media` | 发送单个图片、语音、视频或文件，可附说明、文件名和视频封面 |
| `qq_send_forward` | 发送 QQ 合并转发卡片，混合自建多媒体节点与已有消息引用 |
| `qq_get_message` | 读取已核验消息的文本、发送者、附件类型及可用短期图片引用，不返回附件 URL |
| `qq_get_media` | 同会话按图片引用取图，返回本地路径/MIME/大小；需另用视觉工具读取，不直接调用视觉模型 |
| `qq_get_chat_info` | 查询当前或获准目标会话的名称与类型 |
| `qq_get_recent_messages` | 读取获准群的有界近期消息、发言人、时间、引用及历史状态；需要开启 `group_context` |

这些工具只能在 NapCat 消息触发的实时 Gateway turn 中调用。CLI、TUI、其他平台会话及独立 cron 进程没有当前 QQ 身份，调用时会拒绝执行。Hermes 原有 `send_message` 和 cron 文本投递接口仍可按各自规则工作。

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
  napcat: [napcat_qq]

gateway:
  platforms:
    napcat:
      enabled: true
      extra:
        group_toolsets: [napcat_qq]
        qq_tools:
          enabled: true
          allow_cross_chat: false
        media:
          outbound_roots:
            - /srv/qq-output
```

多人群聊不需要 Agent 主动发图、文件或折叠消息时，保留 `group_toolsets: []`。`qq_tools.enabled` 与 Hermes 工具集授权都必须开启，缺少任意一项时模型无法调用这些动作。

实际主动参与要求 `group_toolsets: []`，因此开启群工具时必须关闭 `proactive_assist` 或保持 `dry_run: true`。自动群背景注入不依赖模型调用工具，无工具模式仍可在被叫到时使用近期历史。

## 读取近期群消息

`qq_get_recent_messages` 还要求 `group_context.enabled: true`。默认读取当前群；返回文字、稳定发言人 ID、时间、引用、附件类型和窗口状态，可包含短期图片引用，不返回媒体 URL，也不读取私人聊天历史。

```json
{"limit": 30}
```

`limit` 不得超过 `group_context.history_limit`；可传入 `before_message_id` 向前读取，但锚点必须来自当前群保留的已核验记录。分页仍受时间窗、返回量和读请求预算约束，不保证完整历史。跨群继续经过下述管理员及目标 ACL 检查。全部配置及数据保留边界见 [GROUP_CHAT](GROUP_CHAT.md)。

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

工具无法跨 profile 借用另一个 QQ 账号的 adapter。引用回复、读取消息及合并转发中的已有消息节点都要通过来源核验。群消息必须属于目标群；私聊消息必须来自目标联系人、是当前入站消息，或是当前进程记住的本机器人已发送消息。无法证明来源时，插件拒绝整次操作。

## 多媒体来源

`source` 接受允许目录内的绝对本地路径、通过 `media.outbound` 策略检查的 HTTP(S) URL，以及同会话 `media:<id>` 图片引用。插件先在 Hermes 侧检查媒体，不把任意 URL 直接交给 NapCat 下载。

已映射的本地文件直接用共享路径；其他已检查媒体可通过 `shared_cache_dir` 复制到专用共享暂存目录，未配置时使用受限 base64。共享映射本身不提供挂载或跨主机同步。基础文档发送仍要求共享路径。出站公网模式、10 MiB 与 WS 预算、共享暂存/配额的完整配置见 [MEDIA](MEDIA.md)。

本地图片受 `media.max_bytes` 和文件头检查约束，同时受 `qq_tools.max_local_media_bytes`（默认 256 MiB）限制；其他本地媒体沿用工具大小上限。没有共享目录时还受 `inline_max_bytes` 约束。

启用 `media.references.enabled` 后，当前图片注释、`qq_get_message` 或群上下文可能提供 `qqimg_...`。`qq_get_media` 参数为 `{"media_id":"qqimg_实际标识"}`，返回本地 `path`、MIME、字节数和 `source`，模型必须通过可用视觉工具消费该路径才能理解像素。自动引用/近期补图则使用正常 Hermes 媒体事件，详见媒体指南。

原样发回示例：

```json
{"media_type":"image","source":"media:qqimg_实际标识"}
```

图片引用绑定当前账号与会话，有期限和撤回检查；一般跨会话发送即使已获管理员授权，也不允许跨聊天使用图片引用。缓存路径不因此加入 `outbound_roots`。重启、到期或淘汰后引用不可用；缓存失效后仅通过原文件标识/安全 URL 再取图。

图片说明与图片放在同一条消息。QQ 客户端对语音、视频和文件混合正文的表现不一致，因此插件先发送媒体，再单独发送说明文字。媒体成功而说明失败时，工具返回 `partial: true` 和已知消息 ID；Agent 不应自动重试整个动作。

插件不转码音视频。NapCat 与 QQ 客户端是否能播放某个编码格式，需要在部署环境验证。

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
  allow_cross_chat: false
  max_segments: 64
  max_media_items: 8
  max_forward_nodes: 50
  max_forward_chars: 50000
  max_local_media_bytes: 268435456
```

相同 session、当前消息和参数的并发调用会共用同一个正在执行的 action，避免并行工具调度产生重复发送。操作完成后不保留幂等缓存，后续相同请求仍可再次发送。写入后超时或断线会返回 `delivery_uncertain: true`，此时应先检查 QQ 会话。
