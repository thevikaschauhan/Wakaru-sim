# Shared integration contracts

Engine is the canonical source for `bundle.json`, `fixtures.json` and `manifest.json`. Copies in Engine, Wakaru, Inkwell and Web must remain byte-identical. Do not edit one consumer copy independently. Run `python3 contracts/integrations/v1/verify.py` and the language conformance tests before accepting a change. CI checks the manifest and reader fixtures locally; release review must also compare all four manifests.

The bundle contains strict versioned schemas and legacy readers. Existing production APIs remain v1. New context/plan readers validate hashes and caller-supplied tenant/subject/time binding but grant no send authority. Live ingestion and email use in the connection contract are fixed false in this foundation release.

Canonical design: Engine `docs/integration-layer-technical-design.md`. Implementation and deployment-role limits: Engine `docs/integration-foundation-c1-c2.md`. [Open the canonical design](https://github.com/thevikaschauhan/vakaru-engine/blob/codex/integration-foundation/docs/integration-layer-technical-design.md).

Go: `contracts/integrations/v1.Read(raw, Binding{...})`. Python: `cart_recovery.intelligence.integration_contracts.read_contract`. TypeScript: `lib/integration-contracts.readIntegrationContract`. Supply authenticated bindings, not values copied from untrusted input. Future service endpoint adoption is C6/C8.
