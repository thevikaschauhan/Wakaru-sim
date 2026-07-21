# Backend schema — store memory (issue #61)

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61)
**Companion docs:** [PRD](./PRD.md), [TDD](./TDD.md)

Four schema surfaces: the Zep ontology (§2), the Redis ledger (§3), the
episode payloads (§4), and the internal API + engine outbox contract (§5).

## 1. Identity

- Store graph id: `merchant_<uuid hex, 32 chars>` — pure function of the
  request-bound merchant UUID (`validate_merchant_id`, `paths.py:52`), charset
  `[a-z0-9_]`. Rebuild generations (TDD §7 fallback only) append `_r<N>`.
- Shopper identity inside a graph: the engine's tenant-scoped pseudonymous
  `anonymous_id` (engine migration 035). Never email, name, address, or phone.
- Sweeper interlock: `merchant_*` ids can never match #72's
  `^mirofish_[0-9a-f]{16}$` sweep filter.

## 2. Zep ontology `cr-v1` (fixed, code-defined; TDD §3)

Shape is exactly the `{entity_types, edge_types}` structure
`GraphBuilderService.set_ontology` already consumes (`graph_builder.py:136-223`);
attribute names respect the existing reserved-name guard (`:148`).

### Entity types

| Type | Attributes (all optional text unless noted) | Notes |
|---|---|---|
| `Shopper` | `anonymous_id`, `first_seen_at`, `abandonment_count`, `recovery_count`, `price_sensitivity_signal` | One per pseudonymous shopper |
| `Product` | `product_id`, `title`, `price_band` (`low\|mid\|high` relative to store), `category` | |
| `CartEpisode` | `episode_key` (idempotency key, §4), `episode_type` (`checkout\|cart`), `cart_value`, `item_count`, `abandoned_at`, `ontology_code` (PRICE_SENSITIVITY, SHIPPING_COST, … — the engine's existing code set from `cart_abandonment_analysis.ontology_code`), `confidence` | One per analyzed abandonment |
| `RecoveryAttempt` | `sent_at`, `angle` (the insight's `recommended_angle`), `discount_offered` | |
| `RecoveryOutcome` | `outcome` (`recovered\|expired`), `resolved_at`, `order_value`, `time_to_recovery_hours` | |

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

## 3. Redis ledger (extends #72's `zep:` namespace; same durability rule: observability and enumeration, never correctness of a paid run)

| Key | Type | Fields / members | Written |
|---|---|---|---|
| `zep:store:<merchant_id>` | Hash | `graph_id` (current generation), `ontology_version`, `created_at` (epoch s), `episode_count_estimate`, `last_pruned_at` | `ensure_store_graph`, prune job, rebuild |
| `zep:stores` | Set | merchant_ids with a live store graph | Same; the prune job's iteration source |
| `zep:merchant:<merchant_id>:graphs` | Set | Reused from #72 SCHEMA §2.3 — store graph joins its merchant's set, so offboarding remains one enumeration | `ensure_store_graph`, offboarding |

No TTLs on live entries; offboarding deletes all three. If Redis is lost,
`zep:stores` is reconstructible by paging `graph.list_all` for `merchant_*`
ids (a documented recovery command in the prune job module, not a runtime
dependency).

## 4. Episode payloads (Zep `type="json"` episodes)

### 4.1 Cart episode (written by the analysis pipeline, TDD §4.1)

```json
{
  "kind": "cart_episode",
  "episode_key": "<shopify_store_id>:<anonymous_id>:<episode_type>:<checkout_started_at ISO>",
  "episode_type": "checkout",
  "anonymous_id": "a-7f3c…",
  "abandoned_at": "2026-07-18T14:03:22Z",
  "cart_value": 86.40,
  "currency": "USD",
  "item_count": 2,
  "products": [
    {"product_id": "1234", "title": "Linen throw", "price": 48.00, "category": "textiles"}
  ],
  "ontology_code": "SHIPPING_COST",
  "confidence": 0.72,
  "recommended_angle": "free_shipping_threshold"
}
```

`episode_key` mirrors the engine's `abandonment_detections` PK
`(shopify_store_id, anonymous_id, episode_type, checkout_started_at)`
(migrations 034/040) — the same real-world event always produces the same key
on both sides. Zep `created_at` param = `abandoned_at`. **Excluded by
schema:** email, name, address, phone, raw user-agent (PII posture #7/#17;
PRD D5).

### 4.2 Outcome episode (ingested via the endpoint, TDD §4.2)

```json
{
  "kind": "recovery_outcome",
  "episode_key": "<same key as the cart episode it resolves>",
  "anonymous_id": "a-7f3c…",
  "attempt": {"sent_at": "2026-07-18T15:10:00Z", "angle": "free_shipping_threshold", "discount_offered": "none"},
  "outcome": "recovered",
  "resolved_at": "2026-07-19T09:41:07Z",
  "order_value": 86.40,
  "time_to_recovery_hours": 18.5
}
```

`outcome ∈ {recovered, expired}`; for `expired`, `order_value`,
`time_to_recovery_hours` are null and `resolved_at` is the attribution-window
close. Zep `created_at` = `resolved_at`.

## 5. Internal API + engine outbox contract

### 5.1 `POST /api/store-memory/outcomes` (Wakaru, new)

- Auth: existing chain — `X-API-Key` (#10), HMAC body signature with
  `WAKARU_INTERNAL_SECRET` (#11), `X-Merchant-Id` (#24, must validate and
  must match the episode's store). `Idempotency-Key` header **required**:
  recommended value = `episode_key`; scope `outcomes:<merchant_id>` via the
  existing `idempotency.py` (24 h TTL — adequate because the engine outbox
  retries well inside 24 h, and a later duplicate merely re-adds an
  identical-keyed episode the graph semantics tolerate).
- Body: §4.2 payload. Responses: `202` accepted, `400` malformed (PII-free
  error body), `401/403` auth chain, `409` idempotency-pending, `503` Zep
  unavailable (engine outbox retries — a 503 must NOT consume the
  idempotency slot: `release()` on the not-performed path, per the
  `idempotency.py` contract).

### 5.2 `DELETE /api/store-memory/<merchant_id>` (Wakaru, new; offboarding)

Same auth chain; path merchant must equal bound `X-Merchant-Id`. Deletes graph
+ §3 entries. `202` idempotent (repeat ⇒ same result). Called by the engine's
`shop/redact` handling.

### 5.3 Engine outbox (engine repo, separate issue; pattern = migration `036_orders_inkwell_forwarding`)

```sql
CREATE TABLE IF NOT EXISTS wakaru_outcome_forwards (
    id                  BIGSERIAL PRIMARY KEY,
    shopify_store_id    INTEGER NOT NULL REFERENCES shopify_stores(id),
    episode_key         TEXT NOT NULL,
    payload             JSONB NOT NULL,          -- exactly §4.2
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (shopify_store_id, episode_key)       -- one outcome per episode
);
CREATE INDEX IF NOT EXISTS idx_wakaru_outcome_forwards_due
    ON wakaru_outcome_forwards (next_attempt_at) WHERE status = 'pending';
```

Delivery worker: exponential backoff, `status='failed'` after 10 attempts +
alert; a `delivered` row is terminal. Ambiguous outcomes (timeout,
unparseable response) stay `pending` and retry with the **same**
`Idempotency-Key` — never re-keyed (possibly-succeeded discipline).
Outcome derivation (`recovered`/`expired`) happens before insert, from
`orders.matched_anonymous_id` (033) joined to `abandonment_detections`
(034/040) within `ATTRIBUTION_WINDOW_DAYS`.
