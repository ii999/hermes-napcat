# Agent QQ 工具

`napcat_qq` 工具集让 Hermes Agent 使用当前 Gateway 的 NapCat WebSocket 连接操作 QQ。工具不建立第二条连接，也不接受任意 OneBot action 名称。

## 工具清单

| 工具 | 用途 |
| --- | --- |
| `qq_send_message` | 发送文本、图片、@、QQ 表情及引用回复，支持多图和图文交错 |
| `qq_send_media` | 发送单个图片、语音、视频或文件，可附说明、文件名和视频封面 |
| `qq_send_forward` | 发送 QQ 合并转发卡片，混合自建多媒体节点与已有消息引用 |
| `qq_get_message` | 读取一条已核验消息的文本、发送者和附件类型，不返回附件 URL |
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

`qq_get_recent_messages` 还要求 `group_context.enabled: true`。默认读取当前群；返回文字、稳定发言人 ID、时间、引用、附件类型和窗口状态，不返回媒体 URL，也不读取私人聊天历史。

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

`source` 接受两类值：

- `media.outbound_roots` 下的本地文件
- 通过现有媒体 host、DNS、重定向、大小和超时检查的 HTTP(S) URL

插件不会把模型提供的 URL 直接交给 NapCat。它先下载并检查远端内容，然后在 `inline_max_bytes` 范围内转成 base64。大文件使用 `shared_paths` 映射：

```yaml
media:
  outbound_roots: [/srv/qq-output]
  inline_max_bytes: 524288
  shared_paths:
    - hermes: /srv/qq-output
      napcat: /data/hermes-output
```

`max_local_media_bytes` 默认限制 Agent 工具读取 256 MiB 本地文件，可以在 `qq_tools` 下调整。共享目录只负责让两端看到同一份内容，插件不复制或挂载文件。

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

直接消息允许 `text`、`image`、`at` 和 `face` 段。插件拒绝 `@all`，也不会解析文本中的 CQ code。

## 合并转发

`qq_send_forward` 允许两种节点混合出现。

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
