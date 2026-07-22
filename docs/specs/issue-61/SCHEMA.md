# Backend schema — store memory (issue #61)

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61)
**Companion docs:** [PRD](./PRD.md), [TDD](./TDD.md)

> **Revision 2 (2026-07-21):** ontology counters removed (exact values live
> in engine SQL per the PRD's deterministic-source invariant); payloads are
> now normative versioned contracts (strict validation, minor-unit money,
> bounded arrays); `merchant_id` moved into the signed outcome body; the
> engine-side contract is the filed issue vakaru-engine#192; redaction hooks
> reference vakaru-engine#193.
>
> **Revision 3 (2026-07-21):** lifecycle mutations become the signed
> **command contract** (§5.2-5.4: `rebuild` / `redact_customer` / `offboard`
> commands + replay pages + operation status) — the DELETE endpoint is
> removed (the HMAC middleware is POST-only). The envelope gains
> `memory_generation` (engine-ledger-authoritative generation pointer;
> Redis demoted to cache). Redaction watermarks added. Idempotency
> standardized on `attempt_id`.

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
| `zep:store:<merchant_id>` | Hash | `graph_id` (current generation — **cache**; the envelope's `memory_generation` is authoritative and wins on mismatch, with a drift alert; TDD §2), `status` (`provisioning\|ready`), `ontology_version`, `created_at` (epoch s), `last_rebuilt_at`, `episode_count_estimate` | `ensure_store_graph` (readiness barrier, TDD §2), rebuild terminal status (TDD §7), envelope reconcile |
| `zep:store:<merchant_id>:tombstone` | String, no TTL | Redaction timestamp; presence blocks `ensure_store_graph` (PRD §4) | `offboard` command (engine#193-driven) |
| `zep:store:<merchant_id>:watermarks` | Hash | `anonymous_id → cutoff_ts` (RFC 3339); enforced on every episode write and replay page (TDD §4.1) — **cache**; durable source = engine ledger, refreshed by every `rebuild`/`redact_customer` command | `redact_customer` command, rebuild commands |
| `zep:store:op:<operation_id>` | Hash, `EX 30d` | `kind`, `merchant_id`, `status` (`running\|verified_current\|verified_empty\|failed`), `detail`, `updated_at` — backs the §5.4 status GET and `operation_id` idempotency | Commands endpoint, operation progress |
| `zep:stores` | Set | merchant_ids with a live store graph | Provisioning, offboarding; reaper's iteration hint |
| `zep:merchant:<merchant_id>:graphs` | Set | Reused from #72 SCHEMA §2.3 — store generations join the merchant's set | Provisioning, rebuild, offboarding |
| `zep:graph:<graph_id>` | Hash | Same shape as #72 SCHEMA §2.1 with `graph_kind=store` (writer asserts `merchant_*` id) | Provisioning, rebuild, offboarding |

Reconstruction (revision 2, made load-bearing-free in revision 3): every key
above is a cache or coordination record. Durable sources: generation pointer
and operation log = engine operations ledger (#192-E, delivered via envelope
`memory_generation` and commands); tombstones and watermarks = engine
redaction state machine (#193, re-delivered on every relevant command);
graph existence = Zep prefix scan. A flushed Redis therefore delays nothing
privacy-critical and mis-states nothing exact; caches are refreshed by the
next envelope/command, and the stale-generation reaper (TDD §7) plus
envelope-mismatch drift alerts surface any residue.

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
re-derives it). The envelope also carries `memory_generation` (revision 3 —
the engine-ledger generation pointer, TDD §2) and any active redaction
watermarks for context. Zep `created_at` param = `occurred_at`.

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

- Auth: existing chain — `X-API-Key` (#10), HMAC signature with
  `WAKARU_INTERNAL_SECRET` (#11), `X-Merchant-Id` (#24) — **plus** the
  §4.2 body/header merchant match (revision 2). **Signing (plan revision
  2):** the store-memory blueprint implements the #73-style signature
  **from day one** — method + canonical path + tenant + nonce + timestamp
  + body digest, with cross-repo conformance tests — since both sides are
  new code. The legacy cart-recovery endpoints migrate separately under
  issue #73; that migration is not a dependency here.
- `Idempotency-Key` header **required**: value = `attempt.attempt_id`
  (unique per attempt); scope `outcomes:<merchant_id>` via the existing
  `idempotency.py` with **TTL 14 days** (revision 2 — exceeds the outbox
  backoff ceiling; the durable at-most-once layer is §5.5's UNIQUE + terminal
  states, this TTL only covers one delivery sequence).
- Body: §4.2, strictly validated. Responses: `202` accepted; `400` schema
  violation (PII-free error body); `401/403` auth chain or merchant
  mismatch; `409` idempotency-pending; `503` Zep unavailable — **releases**
  the idempotency slot (work not performed); an ambiguous failure after the
  graph write does **not** release it (possibly-succeeded discipline).

### 5.2 `POST /api/store-memory/commands` (Wakaru, new; revision 3 — replaces the r2 DELETE, which the POST-only HMAC middleware could not protect)

Same auth chain as §5.1 (the signed body carries `merchant_id`; 403 on
header mismatch). `Idempotency-Key = operation_id`, scope
`commands:<merchant_id>`, 14-day TTL. Body:

```json
{
  "schema_version": 1,
  "operation_id": "op_4f8a12",
  "kind": "rebuild | redact_customer | offboard",
  "merchant_id": "8b1c5f2e-4a6d-4e0b-9c3a-1f2e3d4c5b6a",
  "memory_generation_next": 3,
  "snapshot_id": "snap_2026-07-21_8b1c",
  "expected_event_count": 1742,
  "retention_cutoff": "2026-01-22T00:00:00Z",
  "watermarks": [{"anonymous_id": "a-7f3c9d", "cutoff": "2026-07-21T10:00:00Z"}]
}
```

`rebuild` requires the snapshot/count/generation fields; `redact_customer`
requires `watermarks` (and implies an immediate rebuild); `offboard`
requires none of them (tombstone-first, all-generation delete, TDD §7).
Responses: `202` (operation accepted/replayed — same operation), `400`
schema violation / unknown `kind`, `401/403` auth or merchant mismatch,
`409` operation in progress with a different payload.

### 5.3 `POST /api/store-memory/replay-pages` (Wakaru, new; the engine#192-E feed)

Same auth chain. One page of a `rebuild` operation's snapshot:
`{schema_version, operation_id, merchant_id, page_no, page_count,
event_count, checksum, events: [§4.1 cart episodes and §4.2 outcomes]}` —
`occurred_at`-ascending, bounded page size (≤ 500 events), idempotent per
`(operation_id, page_no)` (replayed pages are acknowledged, not re-applied).
Watermarks are enforced per episode on apply (TDD §4.1). `409` if the
operation is not in `running` state.

### 5.4 `GET /api/store-memory/operations/<operation_id>` (Wakaru, new; status)

Read-only poll (like the existing jobs GET: X-API-Key + merchant binding
suffice — no body to sign). Returns
`{operation_id, kind, status: running | verified_current | verified_empty |
failed, detail, memory_generation, updated_at}`. Terminal statuses carry the
completion evidence (post-delete re-enumeration result, verified episode
count). The engine's state machine (#193) polls this and re-drives on
`failed`; it marks its own leg complete **only** on the verified terminal
status.

### 5.5 Engine outbox (engine repo — **owned by vakaru-engine#192-D**; pattern = migration `036_orders_inkwell_forwarding`)

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
