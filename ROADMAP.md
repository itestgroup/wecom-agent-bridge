# Roadmap

## 0.1.0-dev: independent baseline

- [x] Independent code snapshot; no imported Git history or production deployment workflow.
- [x] Replace deployment targets and private network defaults with explicit configuration.
- [x] Apache-2.0, bilingual README, deployment/API/limitations documents, contribution and security policies.
- [ ] Publish to the independent repository and verify GitHub CI there.
- [ ] Complete fresh-resource end-to-end acceptance using synthetic messages and attachments.

## Before a stable release

- Coordinate Worker/client download deadlines and avoid repeated full bot-media downloads.
- Provide a deliberate customer-service state migration path for service URL changes.
- Demonstrate a durable deduplicating consumer with overlapping-process coordination.
- Expand English deployment documentation and reproducible acceptance instructions.
- Review third-party notices and run public-release secret scanning on the final Git tree.

## Later, driven by use

- Backlog and storage monitoring; retention policies that preserve deduplication semantics.
- Additional deployment providers only when maintainable and independently tested.

Private deployments and this project have separate release schedules. No automatic mirroring, cherry-picking, merging, or deployment into another repository is provided.
