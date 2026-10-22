# WeCom Agent Bridge

English | [中文](README.md)

A self-hosted bridge between WeCom and AI agents: authenticated callbacks, a durable inbox with explicit ACK, media transfer, and standard-library Python reference clients.

**Status: `0.1.0-dev`, an unpublished development baseline.** This repository evolves independently. It contains test CI only, with no production credentials, cloud resource bindings, or automatic deployment workflows.

## Currently connected: Muse

**[Muse](https://muse.ai/join) is the agent platform currently connected and tested in actual use.** This project grew out of the author's private Muse–WeCom integration: WeCom receives messages, the bridge stores them durably, Muse polls and processes them, and replies go through the corresponding channel. Two-way text, image transfer, and incoming files through WeChat Customer Service have been verified in that private deployment. Bot attachment timing and reliability constraints are documented in [known limitations](docs/limitations.md).

The Python clients can serve as a starting point for other agent platforms, but those integrations have not been acceptance-tested. This independent open-source baseline still requires fresh-environment acceptance, separate from the original private deployment.

### Muse invite code: `6H1BE6`

To try this project with Muse, visit the [Muse sign-up page](https://muse.ai/join), find the invite-code redemption option in Muse settings, and enter the author's code **`6H1BE6`**.

The code establishes a referral relationship. Eligible users may receive rewards offered by Muse's current referral promotion, such as additional Muse tokens for trying agent tasks and this integration. The author may also receive referral rewards. Eligibility, redemption deadlines, amounts, and usage restrictions follow the current rules shown by Muse; this project does not promise a fixed reward amount.

Redemption is optional and does not affect this project's license, deployment, or use. This code is not an API authentication secret.

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
