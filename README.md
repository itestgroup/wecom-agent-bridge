# WeCom Agent Bridge

[English](README.en.md) | 中文

将企业微信消息接入 AI 智能体的自托管桥接服务。提供回调接收、持久化收件箱、显式 ACK、素材收发和 Python 参考客户端；智能体由你选择。

**当前为 `0.1.0-dev` 开源整理基线，尚未发布稳定版本。** 此仓库独立演进，不绑定原项目的生产资源、部署凭证或自动部署。仓库内只有测试 CI；提交代码不会部署云服务。

## 能力与边界

| 通道 | 当前范围 | 状态 |
| --- | --- | --- |
| 企业微信自建应用 | 入向文本、图片；出向文本、图片、文件 | 基础能力 |
| 微信客服 | 通知接收、cursor 同步、文本/图片/文件客户端 | 可选能力，需平台授权 |
| 企业微信智能机器人 URL 回调 | 单聊文本、图片、文件、混合消息；文本流式回复 | 实验能力 |

普通文件不能通过自建应用回调接入。机器人附件依赖平台短期签名链接，不能保证延迟处理后仍可下载。项目不提供个人微信群聊天采集或会话内容存档。

## 架构

- `worker.mjs`：Cloudflare Workers 回调验签、AES 解密、D1 存储、收件和 ACK 接口。
- `bot.mjs`：机器人回调、回复状态、受限附件下载。
- `scf/index.py`：腾讯 SCF 发送与素材/客服接口，企业应用密钥保留在服务端。
- `clients/`：标准库 Python 客户端，包含附件校验、断点恢复及客服 SQLite 状态。

消息读取采用至少一次交付。消费者必须按消息 ID 持久化去重，在任务或附件可靠保存后 ACK。多个 cron 进程需要业务层互斥；附件文件锁不能替代任务去重。发送结果不确定时不应盲目重发。

## 从这里开始

1. 阅读 [部署指南](docs/deployment.md)，创建独立测试资源。
2. 用 [客户端示例](examples/receive_once.py) 拉取消息，默认只显示数量，不 ACK。
3. 阅读 [API 与处理约定](docs/api.md) 和 [已知限制](docs/limitations.md)。

本地验证需要 Node.js 24+、Python 3.9+；curl 和 OpenSSL 用于部分本地传输测试。Python 业务代码无第三方包依赖。

```bash
npm ci
npm test
python -m unittest discover -s tests -p 'test_*.py'
npm run build:check
```

测试使用合成消息和本地服务，不调用真实企业微信发送接口。

## 开发与发布

计划见 [ROADMAP.md](ROADMAP.md)，贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。当前快照尚未进行全新云资源部署验收，不宣称端到端生产可用。

许可证：[Apache-2.0](LICENSE)。维护组织：itestgroup。本项目为独立实现，与腾讯、企业微信、Cloudflare 或任意智能体平台无官方隶属关系。
