# WeCom Agent Bridge

English | [中文](README.md)

A self-hosted bridge between WeCom and AI agents: authenticated callbacks, a durable inbox with explicit ACK, media transfer, and standard-library Python reference clients.

**Status: `0.1.0-dev`, an unpublished development baseline.** This repository evolves independently. It contains test CI only, with no production credentials, cloud resource bindings, or automatic deployment workflows.

## Supported channels

| Channel | Scope | Status |
| --- | --- | --- |
| Internal WeCom application | Incoming text/images; outgoing text/images/files | Core |
| WeChat Customer Service | Notifications, cursor-based sync, text/images/files | Optional; requires authorization |
| WeCom AI bot URL callback | Personal single-chat callbacks, attachments, text stream replies | Experimental |

Internal application callbacks do not deliver ordinary files. Bot attachments use short-lived signed URLs and may expire before processing. Personal WeChat group collection and compliance archiving are outside scope.

## Components

- Cloudflare Workers + D1: encrypted callback verification, durable receipts, explicit ACK.
- Tencent SCF: outbound messages, media APIs, and customer-service APIs.
- Python clients: local SQLite customer-service state and checksum-verified resumable downloads.

Delivery is at least once. Consumers must durably deduplicate by message ID and ACK only after durable receipt. Overlapping jobs need application-level coordination. Attachment locks do not prevent duplicate agent tasks. Never blindly repeat a send whose outcome is unknown.

## Getting started

See the [deployment guide](docs/deployment.md) and [API contracts](docs/api.md) (currently Chinese). Set your own service URLs and secrets as environment variables; `.env.example` is documentation, not an automatically loaded configuration file. `examples/receive_once.py` prints counts only and never acknowledges messages.

Local checks require Node.js 24+ and Python 3.9+. Some transport tests use curl and OpenSSL.

```bash
npm ci
npm test
python -m unittest discover -s tests -p 'test_*.py'
npm run build:check
```

Read the [known limitations](docs/limitations.md), [roadmap](ROADMAP.md), [contribution guide](CONTRIBUTING.md), and [security policy](SECURITY.md). Fresh cloud deployment acceptance has not yet been completed for this open-source baseline.

Licensed under [Apache-2.0](LICENSE). Maintained by itestgroup. This independent project is not affiliated with Tencent, WeCom, Cloudflare, or an agent platform.
