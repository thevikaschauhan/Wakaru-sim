# Backend schema — store memory (issue #61)

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61)
**Companion docs:** [PRD](./PRD.md), [TDD](./TDD.md)

> **Revision 2 (2026-07-21):** ontology counters removed (exact values live
> in engine SQL per the PRD's deterministic-source invariant); payloads are
> now normative versioned contracts (strict validation, minor-unit money,
> bounded arrays); `merchant_id` moved into the signed outcome body; the
> engine-side contract is the filed issue vakaru-engine#192; redaction hooks
> reference vakaru-engine#193.

Four schema surfaces: the Zep ontology (§2), the Redis ledger (§3), the
episode payloads (§4), and the internal API + engine outbox contract (§5).

## 1. Identity

- Store graph id: `merchant_<uuid hex, 32 chars>` for generation 0,
  `merchant_<hex32>_r<N>` for rebuild generations — a pure function of the
  request-bound merchant UUID (`validate_merchant_id`, `paths.py:52`) plus
  the ledger's generation pointer; charset `[a-z0-9_]`. All generations are
  enumerable from Zep by prefix `merchant_<hex32>` — privacy operations
  never depend on Redis (TDD §7).
- Shopper identity inside a graph: the engine's tenant-scoped pseudonymous
  `anonymous_id` (engine migration 035), delivered via the #192-A envelope.
  Never email, name, address, or phone.
- Sweeper interlock: `merchant_*` ids can never match #72's
  `^mirofish_[0-9a-f]{16}$` sweep filter, and #72's scratch-registry writer
  asserts `graph_kind=scratch` (#72 SCHEMA §2.1/2.2) — two independent
  structural guards.

## 2. Zep ontology `cr-v1` (fixed, code-defined; TDD §3)

Shape is exactly the `{entity_types, edge_types}` structure
`GraphBuilderService.set_ontology` already consumes (`graph_builder.py:136-223`);
attribute names respect the existing reserved-name guard (`:148`).

**Revision 2 — qualitative only.** Removed from the previous revision:
`Shopper.abandonment_count`, `Shopper.recovery_count` (exact counters cannot
be maintained by LLM entity extraction and are owned by engine SQL;
delivered per-request in the envelope's `StorePriors`).

### Entity types

| Type | Attributes (all optional text) | Notes |
|---|---|---|
| `Shopper` | `anonymous_id`, `first_seen_at`, `price_sensitivity_signal` (qualitative descriptor) | One per pseudonymous shopper; uniqueness is *semantic* (Zep entity resolution), which is acceptable because nothing exact is computed from it |
| `Product` | `product_id`, `title`, `price_band` (`low\|mid\|high` relative to store), `category` | |
| `CartEpisode` | `event_id` (envelope key, §4), `episode_type` (`checkout\|cart`), `abandoned_at`, `ontology_code` (engine's existing code set from `cart_abandonment_analysis.ontology_code`), `narrative` (short qualitative summary) | One per analyzed abandonment |
| `RecoveryAttempt` | `attempt_id`, `sent_at`, `angle` (actually sent, per engine#192-C), `discount_offered` | |
| `RecoveryOutcome` | `outcome` (`recovered\|expired`), `resolved_at` | |

### Edge types

| Edge | Source → Target | Meaning |
|---|---|---|
| `ABANDONED` | Shopper → CartEpisode | |
| `CONTAINS` | CartEpisode → Product | |
| `TARGETED_BY` | CartEpisode → RecoveryAttempt | |
| `RESULTED_IN` | RecoveryAttempt → RecoveryOutcome | |
| `RECOVERED_AS` | RecoveryOutcome → Product | Present only on `recovered`, per purchased product |

Version literal: `ONTOLOGY_VERSION = "cr-v1"`. Evolution: additive ⇒ `cr-v2`
lazy re-apply; rename/remove ⇒ rebuild generation (TDD §3, §7).

## 3. Redis ledger (extends the `zep:` namespace; observability, coordination, and reconstruction-friendly cache — never the privacy-operation source of truth)

| Key | Type | Fields / members | Written |
|---|---|---|---|
| `zep:store:<merchant_id>` | Hash | `graph_id` (current generation), `status` (`provisioning\|ready`), `ontology_version`, `created_at` (epoch s), `last_rebuilt_at`, `episode_count_estimate` | `ensure_store_graph` (readiness barrier, TDD §2), rebuild flip (TDD §7) |
| `zep:store:<merchant_id>:tombstone` | String, no TTL | Redaction timestamp; presence blocks `ensure_store_graph` (PRD §4) | Offboarding (engine#193-driven) |
| `zep:stores` | Set | merchant_ids with a live store graph | Provisioning, offboarding; rebuild job's iteration source |
| `zep:merchant:<merchant_id>:graphs` | Set | Reused from #72 SCHEMA §2.3 — store generations join the merchant's set | Provisioning, rebuild, offboarding |
| `zep:graph:<graph_id>` | Hash | Same shape as #72 SCHEMA §2.1 with `graph_kind=store` (writer asserts `merchant_*` id) | Provisioning, rebuild, offboarding |

Reconstruction (revision 2, explicit): all of the above except the tombstone
are rebuildable from a Zep `list_all` prefix scan + the engine ledger; the
tombstone's durable source is the engine's redaction state machine
(engine#193), which re-drives Wakaru until verified — so a flushed Redis
delays nothing privacy-critical. The rebuild job reconciles `zep:stores`
against the prefix scan each cycle and alerts on drift.

## 4. Episode payloads — **normative contracts** (revision 2)

Versioned JSON Schema documents live in the repo
(`docs/specs/issue-61/contracts/cart_episode.v1.json`,
`recovery_outcome.v1.json` — added with the implementation PR) and are
enforced with strict validation (`additionalProperties: false`) at the
producing and consuming ends. Rules for both:

- `schema_version` (integer, required) — additive evolution only; a consumer
  rejects versions it does not know.
- Timestamps: RFC 3339 UTC (`Z` suffix), required precision seconds.
- Money: **integer minor units** + ISO-4217 `currency` (revision 2 — no
  floats).
- Arrays bounded (`products` ≤ 50); strings bounded (titles ≤ 200 chars).
- Enums closed: `episode_type ∈ {checkout, cart}`,
  `outcome ∈ {recovered, expired}`.
- **Excluded by schema:** email, name, address, phone, raw user-agent, free
  URLs (PII posture #7/#17; PRD D5).

### 4.1 Cart episode (written by the analysis pipeline from the engine#192-A envelope)

```json
{
  "schema_version": 1,
  "kind": "cart_episode",
  "event_id": "12345:a-7f3c9d:checkout:2026-07-18T13:41:00Z",
  "merchant_id": "8b1c5f2e-4a6d-4e0b-9c3a-1f2e3d4c5b6a",
  "shopify_store_id": 12345,
  "anonymous_id": "a-7f3c9d",
  "episode_type": "checkout",
  "checkout_started_at": "2026-07-18T13:41:00Z",
  "occurred_at": "2026-07-18T14:03:22Z",
  "cart_value_minor": 8640,
  "currency": "USD",
  "item_count": 2,
  "products": [
    {"product_id": "1234", "title": "Linen throw", "price_minor": 4800, "category": "textiles"}
  ],
  "ontology_code": "SHIPPING_COST",
  "confidence": 0.72,
  "recommended_angle": "free_shipping_threshold"
}
```

`event_id` = `<shopify_store_id>:<anonymous_id>:<episode_type>:<checkout_started_at>`
— mirrors the engine's `abandonment_detections` PK (migrations 034/040) and
arrives **pre-built in the envelope** (engine#192-A; Wakaru never
re-derives it). Zep `created_at` param = `occurred_at`.

### 4.2 Recovery-outcome episode (ingested via the endpoint; engine#192-C/D producer)

```json
{
  "schema_version": 1,
  "kind": "recovery_outcome",
  "event_id": "12345:a-7f3c9d:checkout:2026-07-18T13:41:00Z",
  "merchant_id": "8b1c5f2e-4a6d-4e0b-9c3a-1f2e3d4c5b6a",
  "anonymous_id": "a-7f3c9d",
  "attempt": {
    "attempt_id": "att_9c2e77",
    "email_document_id": "doc_5a1b2c",
    "sent_at": "2026-07-18T15:10:00Z",
    "angle": "free_shipping_threshold",
    "discount_offered": "none"
  },
  "outcome": "recovered",
  "resolved_at": "2026-07-19T09:41:07Z",
  "order_value_minor": 8640,
  "currency": "USD",
  "time_to_recovery_hours": 18.5
}
```

`merchant_id` in the body is load-bearing (revision 2): it is inside the
HMAC-signed bytes and must equal the bound `X-Merchant-Id` (403 on
mismatch). `attempt` references the durable engine `recovery_attempt` row
(engine#192-C) — outcomes prove exposure, not recommendation. For `expired`:
`order_value_minor`/`time_to_recovery_hours` null, `resolved_at` = the
attribution-window close. Zep `created_at` = `resolved_at`.

## 5. Internal API + engine outbox contract

### 5.1 `POST /api/store-memory/outcomes` (Wakaru, new)

- Auth: existing chain — `X-API-Key` (#10), HMAC body signature with
  `WAKARU_INTERNAL_SECRET` (#11), `X-Merchant-Id` (#24) — **plus** the
  §4.2 body/header merchant match (revision 2; converges with issue #73,
  whose stronger method/path/ts/merchant/body-hash signature this endpoint
  adopts verbatim when it lands).
- `Idempotency-Key` header **required**: value = `attempt.attempt_id`
  (unique per attempt); scope `outcomes:<merchant_id>` via the existing
  `idempotency.py` with **TTL 14 days** (revision 2 — exceeds the outbox
  backoff ceiling; the durable at-most-once layer is §5.3's UNIQUE + terminal
  states, this TTL only covers one delivery sequence).
- Body: §4.2, strictly validated. Responses: `202` accepted; `400` schema
  violation (PII-free error body); `401/403` auth chain or merchant
  mismatch; `409` idempotency-pending; `503` Zep unavailable — **releases**
  the idempotency slot (work not performed); an ambiguous failure after the
  graph write does **not** release it (possibly-succeeded discipline).

### 5.2 `DELETE /api/store-memory/<merchant_id>` (Wakaru, new; offboarding)

Same auth chain; path merchant must equal bound `X-Merchant-Id`. Semantics
(revision 2 — delete-and-verify, tombstone-first): write tombstone → delete
all generations enumerated from Zep by prefix → ledger cleanup → `202` with
`{status: verified_empty | pending, remaining: [...]}`. The engine's
redaction state machine (engine#193) re-calls until `verified_empty`;
idempotent, Zep 404 = success. Customer-level erasure is **not** an endpoint
here: engine#193 erases its ledger rows and the shopper is excluded from the
next (immediately triggered) rebuild — rebuild is the erasure mechanism
(TDD §7).

### 5.3 Engine outbox (engine repo — **owned by vakaru-engine#192-D**; pattern = migration `036_orders_inkwell_forwarding`)

```sql
CREATE TABLE IF NOT EXISTS wakaru_outcome_forwards (
    id                  BIGSERIAL PRIMARY KEY,
    shopify_store_id    INTEGER NOT NULL REFERENCES shopify_stores(id),
    attempt_id          TEXT NOT NULL,           -- engine#192-C recovery_attempt
    episode_key         TEXT NOT NULL,           -- = §4 event_id
    payload             JSONB NOT NULL,          -- exactly §4.2
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (shopify_store_id, attempt_id)        -- one outcome per attempt
);
CREATE INDEX IF NOT EXISTS idx_wakaru_outcome_forwards_due
    ON wakaru_outcome_forwards (next_attempt_at) WHERE status = 'pending';
```

Delivery worker: exponential backoff with a ceiling **under 14 days**
(matching §5.1's idempotency TTL), `status='failed'` after 10 attempts +
alert; a `delivered` row is terminal. Ambiguous outcomes (timeout,
unparseable response) stay `pending` and retry with the **same**
`Idempotency-Key` — never re-keyed (possibly-succeeded discipline). Outcome
derivation (`recovered`/`expired`) happens before insert, from
`orders.matched_anonymous_id` (033) joined to the attempt's episode
(034/040) within `ATTRIBUTION_WINDOW_DAYS`. Final DDL and the attempt table
itself live with engine#192.
