# Integration foundation reference

The canonical technical design is maintained in the Engine repository at `docs/integration-layer-technical-design.md`. C1/C2 implementation decisions, migration boundaries and acceptance evidence are in `docs/integration-foundation-c1-c2.md` in Engine. Keep one design authority; update that document when changing cross-service invariants.

Canonical references: [technical design](https://github.com/thevikaschauhan/vakaru-engine/blob/codex/integration-foundation/docs/integration-layer-technical-design.md), [implementation report](https://github.com/thevikaschauhan/vakaru-engine/blob/codex/integration-foundation/docs/integration-foundation-c1-c2.md), [review fixes](https://github.com/thevikaschauhan/vakaru-engine/blob/codex/integration-foundation/docs/integration-foundation-review.md).

This repository contains the identical integration contract bundle and a language reader. Production context/composition APIs remain v1; no new live ingestion, deployment or email sending is activated. See `contracts/integrations/v1/README.md` for validation commands and compatibility boundaries.
