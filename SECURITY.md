# Security policy

This development baseline has no stable supported release yet. Do not publish credentials, exploitable payloads, personal data, signed download URLs, or databases in public issues.

Use the repository's private vulnerability reporting feature if enabled. If unavailable, open a minimal issue requesting a private contact channel without disclosing the vulnerability. No response-time SLA is promised.

Deployers control credentials, cloud access, WeCom authorization, source-host allowlists and data retention. Use separate test resources and credentials. Rotate exposed credentials before removing them from history; history deletion cannot revoke a copied secret.

No actual cloud deployment bindings or credentials are provided by this repository. Incoming and outgoing message contents may contain sensitive customer data; keep local state and backups under appropriate access controls.
