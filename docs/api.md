# API 与处理约定

除平台回调及 Worker `/health` 外，Worker 接口使用 `Authorization: Bearer <INBOX_SECRET>`。SCF 全部接口使用 `X-Push-Secret: <PUSH_SECRET>`。生产环境使用 HTTPS。密钥不要放在 URL 查询参数中。

| 服务 | 方法/路径 | 用途 |
| --- | --- | --- |
| Worker | GET/POST `/wecom` | 企业微信 URL 验证、加密 XML 回调 |
| Worker | GET `/inbox?limit=50&include_media=1` | 非破坏读取文本和媒体元数据 |
| Worker | POST `/inbox/ack` | `{"ids":["message-id"]}`，收到 missing 时检查处理结果 |
| Worker | GET `/kf/events` | 客服同步通知，包含短期 token，不应记录原始响应 |
| Worker | POST `/kf/events/ack` | 客服通知 ACK |
| Worker | GET/POST `/bot` | 机器人验证、加密 JSON 回调和流刷新 |
| Worker | GET `/bot/identities`、`/bot/inbox`、`/bot/failures` | 身份核对、待处理消息、需要重发的记录 |
| Worker | POST `/bot/ack` | 机器人消息 ACK；过期附件记录不可作为成功 ACK |
| Worker | POST `/bot/reply` | `{"id":"message-id","text":"reply"}`，保存最终回复，不代表已显示 |
| Worker | POST `/bot/media` | `id,index,offset,chunk_size` 下载附件块 |
| SCF | GET `/health` | 版本和功能开关 |
| SCF | POST `/` 或 `/send` | 文本 `text,touser?`；或 `msgtype,media_id,touser?` |
| SCF | POST `/media/upload` | `type,filename,content_base64` 或允许主机上的 `source_url` |
| SCF | POST `/media/download` | `media_id,offset?,chunk_size?` |
| SCF | POST `/kf/accounts`、`/kf/sync`、`/kf/send` | 账号、cursor 同步和客服回复 |

客户端具体参数见 `clients/`。普通 inbox 最多返回 50 条，`has_more` 表示还有待取消息。所有消费者必须持久化去重。

附件顺序：保存消息 → 下载并校验 → 持久化接收结果或任务 → ACK。`AttachmentExpired` 是媒体与机器人共用的异常，`retryable=False`，状态 `needs_resend`；保留原始记录，要求发送者重新发送，不反复下载旧链接。

发送接口可能返回 HTTP 200 且 `ok:false`，调用者必须检查业务结果。`delivery_unknown` 表示发送结果未知；不得无条件重发。机器人 `stored:true`、`delivery_confirmed:false` 只表明回复已存储。
