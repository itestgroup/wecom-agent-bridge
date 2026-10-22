# Contributing

Use a focused branch and pull request. Explain the observable problem, the resulting behavior, and relevant verification. Never include real tokens, callback ciphertext, signed media URLs, private message contents, or client databases in an issue or fixture.

Run `npm ci`, `npm test`, `npm run build:check`, and `python -m unittest discover -s tests -p 'test_*.py'`. Network-sensitive tests use synthetic data and local servers. Do not add tests that send messages to real accounts.

Preserve explicit ACK after durable receipt, shared AttachmentExpired semantics, no blind retries of sends, and the existing explicit recipient/environment/default order. If a contract must change, document a migration.

CI performs checks only. Cloud resources and deployment credentials belong to each deployer and must not be committed. Community changes do not automatically update private deployments.

By contributing, you agree your contribution is available under this repository's Apache-2.0 license. Confirm you have the right to contribute any third-party material and preserve required notices.
