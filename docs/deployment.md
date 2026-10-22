# 独立测试部署

本仓库没有自动部署工作流。请使用自己的账号及新建的 Worker、D1、SCF、企业微信测试应用；不要复用其他部署的配置、凭证或数据库。文档是开发版部署路径，正式发布前仍需全新环境验收。

## 1. Worker 与 D1

需要 Node.js 24+、Python 3.9+、Cloudflare 账号和企业微信自建应用。

```bash
npm ci
npx wrangler login
npx wrangler d1 create wecom-agent-bridge-test
python scripts/configure_worker.py
```

编辑被 Git 忽略的 `wrangler.json`：设置自己的测试 Worker 名字、D1 名字及刚创建的 UUID。模板没有可用的数据库 ID。默认关闭 workers.dev；需要测试地址时自行设置 `workers_dev: true`，或在控制台绑定自己的域名。不要将本仓库接入已有生产服务的 Git Builds。

以下命令中的数据库名应与自己的本地配置一致：

```bash
npx wrangler d1 execute wecom-agent-bridge-test --remote --file schema.sql
npx wrangler d1 execute wecom-agent-bridge-test --remote --file migrations/0001_media_messages.sql
npx wrangler d1 execute wecom-agent-bridge-test --remote --file migrations/0002_kf_notifications.sql
npx wrangler d1 execute wecom-agent-bridge-test --remote --file migrations/0003_bot_messages.sql
```

在本地 `wrangler.json` 的 `vars` 或 Worker 控制台配置文本变量：

| 变量 | 用途 |
| --- | --- |
| WECOM_CORP_ID | 测试企业 ID |
| WECOM_AGENT_ID | 测试应用 ID，建议明确设置 |
| ALLOWED_USER_IDS | 已核对的内部用户 ID，逗号分隔；空值允许应用可见范围内的发送者 |
| MEDIA_ENABLED | 完成媒体迁移后才设为 `true` |
| KF_ENABLED | 初期 `false` |
| BOT_ENABLED | 初期 `false` |

用交互命令写入 Worker 密钥；不要将真实值写进仓库：

```bash
npx wrangler secret put WECOM_TOKEN
npx wrangler secret put WECOM_AES_KEY
npx wrangler secret put INBOX_SECRET
python scripts/configure_worker.py --check
npm run deploy
```

`WECOM_TOKEN` 和 43 字符 `WECOM_AES_KEY` 与企业微信回调页面一致；`INBOX_SECRET` 至少 32 个随机字符。回调 URL 为自己的 `https://<worker>/wecom`。先通过 URL 验证，再发送测试消息。

`GET /health` 只报告服务版本和开关，不代表 D1、SCF 或企业微信端到端健康。

## 2. SCF

创建独立的 Python 3.9+ 测试函数，把 `scf/index.py` 上传为根目录 `index.py`，入口 `index.main_handler`。配置自己的函数 URL、固定公网出口和企业微信可信 IP。

环境变量：`WECOM_CORPID`、`WECOM_AGENTID`、`WECOM_SECRET`、`WECOM_TOUSER`、`PUSH_SECRET`。Worker 的 `WECOM_CORP_ID` 与 SCF 的 `WECOM_CORPID` 拼写不同。调用使用 `X-Push-Secret`。

保留原有收件人语义：调用方 `touser` 优先，否则用 `WECOM_TOUSER`，再默认 `@all`。测试环境建议明确设置自己核对过的接收人。

可选脚本 `scripts/deploy_scf.py` 只更新已存在函数的代码。使用前显式设置 `SCF_REGION`、`SCF_NAMESPACE`、`SCF_FUNCTION_NAME`、`SCF_URL_QUALIFIER` 和腾讯云凭证，再安装 `scripts/requirements-deploy.txt`。没有任何默认生产目标。脚本不会创建函数、网络、别名或部署密钥。

## 3. 客户端

在智能体运行环境导出 `.env.example` 中自己的变量；Python 不会自动读取此文件。默认直连；需要代理时设置 `HTTPS_PROXY` 或 `https_proxy`，遵循系统 `NO_PROXY` 行为。

```bash
python examples/receive_once.py
```

先持久化消息或下游任务，再显式 ACK。把消息 ID 用作业务幂等键。不要对发送接口增加通用重试。

## 4. 可选通道

微信客服：先在企业微信授权测试应用管理自己的客服账号。在 Worker 与 SCF 均设置 `KF_ENABLED=true`、`KF_OPEN_KFID`；回调仍是 `/wecom`。`KfClient` 使用持久化状态目录；核对 `identities()` 后才设置用户允许列表。不得删除 SQLite 或盲目更换其绑定的服务 URL。

机器人：Worker 设置 `BOT_ENABLED=true`、独立的 `BOT_TOKEN`/`BOT_AES_KEY`，回调 URL 为 `/bot`。用 `/bot/identities` 核对身份后设置 `BOT_ID` 和 `BOT_ALLOWED_USER_IDS`。附件仅允许 `BOT_MEDIA_HOSTS` 中明确列出的实际 HTTPS 主机；不要放开任意 URL。智能体只需要 inbox 凭证，不需要机器人 AES 密钥。

## 验收与升级

每个独立环境验证：回调成功；重复读取同一消息；本地持久化后 ACK；重放不复活；图片文件校验；重启保留状态；失败不假成功。测试材料用虚构内容。

升级前备份 D1 和客户端 SQLite。先在测试环境应用必要迁移、测试新代码，再决定自己的生产发布；本仓库不自动同步任何私有生产仓库。
