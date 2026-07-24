# PRD — Per-store persistent Zep knowledge graph ("store memory") with outcome feedback

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61) (P2, enhancement)
**Depends on:** #72 (must land first); vakaru-engine#192 (event/attempt/outcome contract — the engine half of this design); vakaru-engine#193 (redaction state machine)
**Related:** #24 (merchant binding — landed), #73 (tenant identity in signatures), vakaru-engine#191 (GDPR log PII bug, filed during this spec's review)
**Companion docs:** [TDD](./TDD.md), [SCHEMA](./SCHEMA.md)
**Status:** Proposed (revision 2)
**Verified against:** Wakaru `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0`; vakaru-engine @ `1bb1d95` (handlers + migrations `001,033,034,035,036,040`)

> **Revision 2 (2026-07-21), after external design review.** The architecture
> boundary moved: the **engine's relational ledger** now owns everything that
> must be exact (identity, attempts, outcomes, counts, rates, retention
> index), and the Zep store graph is **bounded qualitative context only**.
> Deterministic store priors are computed in engine SQL and shipped in the
> request envelope, not derived from semantic graph search. Retention is
> enforced primarily by whole-graph rebuild from the engine's event ledger.
> Redaction (shop **and** customer level) is a durable state machine with
> tombstones. The pilot evaluation is randomized/exposure-attributed. Engine
> prerequisites are now explicit filed issues (#192, #193).
>
> **Revision 3 (2026-07-21), after review-round 2.** All lifecycle mutations
> (rebuild, customer redaction, offboarding) are now **signed POST commands
> from the engine** with operation ids, cutoff watermarks, status polling,
> and verified completion — one design move that closes four of the round's
> five blockers: the customer-redaction race gets a per-shopper watermark
> enforced on every write and replay; the rebuild/replay feed becomes a
> normative engine#192-E contract (engine-driven, matching the existing
> call topology); the unprotectable DELETE endpoint is gone (Wakaru's HMAC
> middleware is POST-only and signs only timestamp+body — verified); and the
> current-generation pointer moves to the engine ledger, delivered on every
> analyze request in the envelope, demoting Redis to a reconcilable cache.
> The idempotency key is standardized on `attempt_id` across all documents
> and engine#192.

## 1. Product thesis

Today each analysis is amnesiac: a throwaway graph holds one cart, the
simulation runs, the insight ships, everything is destroyed (#72 makes the
destruction actually happen). Nothing Vakaru learns about a store's shoppers in
January improves the email it writes in March.

**The goal of #61 is that every additional abandonment a store processes makes
the next analysis measurably better.** That requires two things, and this PRD
deliberately scopes both, because one without the other does not compound:

- **Track M (memory):** one persistent Zep graph per merchant that accumulates
  the store's abandonment history — Model B from the issue — as
  **qualitative context** for personas and reports.
- **Track O (outcomes):** recovery *results* flow back. A memory containing
  only questions (carts abandoned) cannot make the system smarter; the
  compounding signal is the answers (recovered or not, by which angle
  actually sent, at what discount). The engine already holds the raw data
  (`orders.matched_anonymous_id`, migration 033; `abandonment_detections`,
  migrations 034/040) but today records **no durable link between an episode
  and an actual send** (verified: `handlers/email_send.go` receives only
  `{email_document_id, to, subject, html, text}`) — building that link is
  vakaru-engine#192-C.

### The deterministic-source invariant (revision 2, load-bearing)

> **Any value that must be complete, exact, unique, auditable, or
> deletable-by-deadline is computed from the engine's relational ledger and
> delivered to Wakaru as data. It is never derived from semantic graph
> retrieval.** Zep search is relevance-ranked and bounded — a sample, not a
> population — so counts, rates, priors, identity, and retention state do not
> come from it. The graph supplies qualitative texture: what similar
> abandonments looked like, how a known shopper hesitated before, what
> narrative context fits this cart.

This invariant resolves what the external review correctly identified as the
original spec's central flaw (outcome rates computed from `graph.search`
top-K results would be biased and unstable, and entity attributes maintained
by LLM extraction cannot hold exact counters).

Intelligence-over-time claim, stated falsifiably: with Track M+O live, the
per-store recovery-conversion rate for treated merchants should exceed a
concurrent randomized control (§7). If it does not, the feature is killed at
the Phase-4 gate rather than kept as cost.

## 2. What memory concretely changes in the product

| Capability | Without store memory (today) | With store memory (source per the invariant) |
|---|---|---|
| Repeat abandoner | Invisible — every cart is a stranger | Flag + counts from **engine SQL** (in the envelope); the shopper's prior *narratives* from the graph |
| Store-level price sensitivity | Re-inferred from one cart | Distribution of abandonment reasons from **engine SQL**; illustrative episodes from the graph |
| What wins carts back *here* | Unknown | Recovery rate by actually-sent angle/discount from **engine SQL** (exposure-attributed via #192-C attempts) |
| Product-level abandonment pattern | Invisible | Counts from **engine SQL**; product-context edges from the graph |
| Email angle selection | Heuristic + single-cart LLM read | Grounded in priors (SQL) + qualitative store memory (graph) via personas and report context |

## 3. Decisions on the issue's nine design questions

These are decisions, not options. Rationale in TDD; listed here for product
sign-off. (D3/D4/D9 revised per the external review.)

| # | Question (from #61) | Decision |
|---|---|---|
| D1 | Ontology per store, drift? | **Fixed, versioned, code-defined cart-recovery ontology** (SCHEMA §2) replaces per-event LLM ontology for store graphs. Additive evolution only; version stamped in the ledger. Removes drift, removes one LLM call per analysis. |
| D2 | Stateless invariant | **Repealed for the graph layer only**, by design and documented in CLAUDE.md as part of the implementation PR. Local project/sim/report scratch stays per-run and per-run-deleted (#24 CP2b unchanged). |
| D3 | Store-wide graph diluting a fresh cart's personas | **No analysis-path read may enumerate a store graph.** Revision 2 hardening: the current ReportAgent toolset includes `panorama_search`, which calls `get_all_nodes`/`get_all_edges` (`zep_tools.py:1146,651,679`) — on a store graph that is a full-history scan. Store-graph reads go through a bounded adapter (query-scoped `graph.search`, hard result caps); panorama and every full-scan tool are disabled for `merchant_*` graphs, enforced by tests that fail on any list-all SDK call (TDD §5). |
| D4 | Retention/pruning | Rolling **180-day episode retention** for active merchants. **Primary enforcement = periodic whole-graph rebuild from the engine's retained event ledger** (revision 2 — the Zep episode reader is `lastn`-only with no time filter, and episode-delete's effect on derived artifacts is unproven; rebuild-and-verify *proves* expiry). Opportunistic episode-delete may supplement once V-1 verifies its semantics. Per-graph episode cap 20,000. |
| D5 | PII at rest | Accepted deliberately for active merchants under D4's window, documented with the Zep DPA reference; shopper identifiers stored are the engine's pseudonymous `anonymous_id` (migration 035), never email/name. Note (plan-r3 correction): today's Wakaru-bound payload **requires** `email`/`customer_name` (`ShopifyCartData` required fields), so an additive envelope alone cannot remove them — removal is the three-step #192-A migration: A1 adds the pseudonymous fields → a Wakaru PR stops requiring email/name (neutral seed-formatter defaults) → A3 removes them from the engine payload with a wire-capture test proving absence. The pseudonymization claim is true only after A3. |
| D6 | Zep account/plan limits | Graph count = active merchant count (plus in-flight `mirofish_*`). Operator confirms plan headroom before Phase 3 (rollout checklist). Per-graph episode count is a tracked metric. |
| D7 | Provisioning/lifecycle | **Lazy, idempotent create-on-first-abandonment** keyed `merchant_<uuid-hex-32>`, with a **readiness barrier**: a graph is writable only after its ontology is applied and its ledger record says `ready` (revision 2 — closes the loser-before-ontology race). Offboarding deletes the graph and writes a **tombstone** that blocks re-creation (see §4). |
| D8 | Migration of `mirofish_*` orphans | Already owned by #72's sweeper; #61 adds nothing and its `merchant_*` graphs are structurally unsweepable (`^mirofish_[0-9a-f]{16}$` filter + registry separation, #72 SCHEMA §2.1/2.2). |
| D9 | Concurrent same-store writes | Zep episode ingestion is server-side async per graph; the provisioning race is resolved by the D7 readiness barrier (status-gated, bounded wait), not by luck. No cross-request locking in the steady-state hot path. |

## 4. Redaction and offboarding (revision 2 — supersedes the issue's acceptance draft)

The issue's last acceptance bullet says: *"Merchant offboarding doesn't deletes
the store graph, we will retain the data for 180 days."* This remains
**overruled**: Shopify's mandatory privacy webhooks require erasure. The
external review additionally showed the first revision was not strong enough:
the engine's `shop/redact` and `customers/redact` handlers are **stubs**
(verified `handlers/shopify_webhooks.go`, engine @ `1bb1d95`), a deleted
graph could be silently recreated by a concurrent analysis, and customer-level
erasure was unaddressed.

Adopted policy (revision 3 — every flow below is a **signed POST command**
from the engine's state machine to Wakaru's single command endpoint, with an
`operation_id`, idempotent replay, status polling, and completion only on
verified evidence; contract in SCHEMA §5.2-5.4, engine side in #193's
amendment):

- **Active merchant:** rolling 180-day episode retention, enforced by
  engine-driven rebuild (D4; replay feed = engine#192-E).
- **Uninstalled merchant (shop/redact ⇒ `offboard` command):** Wakaru writes
  the **merchant tombstone first — durably, as a metadata-only marker graph
  in Zep itself** (plan-r4: a Redis-only tombstone did not survive a flush,
  so a queued pre-uninstall job could recreate erased data; checked by
  `ensure_store_graph` before any create), then deletes **all generations**
  enumerated from Zep by prefix (never from Redis), then reports terminal
  status `verified_empty` only after a re-enumeration returns nothing but
  the marker — a straggler-recreated graph fails the verify and the engine
  re-drives, and re-verifies once more T+24h after terminal. Incomplete
  redaction past deadline alerts engine-side.
- **Phase-1 erasure floor (plan-r5, W4e — purge-after-drain):** store memory
  never writes a production graph before this deletion path is verified
  end-to-end. Until the rebuild machinery exists, `customers/redact` is
  satisfied by **purge-after-drain**: the engine persists the durable
  customer watermark (#193) first, waits a drain delay
  (`REDACT_DRAIN_HOURS`, default 2 h — deliberately above Wakaru's max
  queue + run latency of ≈ 90 min, so every pre-redaction envelope has
  drained and every later envelope carries the watermark), then dispatches
  a **`purge` command** (new kind; SCHEMA §5.2): delete **all**
  `merchant_<hex32>*` generations, write **no tombstone**, terminal
  `verified_empty` = zero generations remain. The graph is a write-only
  rebuildable cache in Phase 1, so this over-erasure is compliant; the
  graph re-provisions organically on the merchant's next analyzed
  abandonment, and replay (#192-E) can restore a purged — non-tombstoned —
  merchant's retained history once it lands. Dispatch is **unconditional
  on every `customers/redact` webhook** (an idempotent no-op for a
  merchant with no graph) and is never keyed on the store-memory
  allowlist, so a merchant removed from the pilot is still purged. Only
  `shop/redact` writes the permanent tombstone: a tombstoned (offboarded)
  merchant is permanently closed, and replay never restores it.
- **Customer-level erasure (customers/redact):** the engine persists the
  durable per-shopper watermark, then erases via **purge-after-drain**
  (Phase 1, above) or — once rebuild machinery exists — via an immediate
  **`rebuild`**, whose replay excludes the watermarked shopper; **rebuild
  is the erasure mechanism**, and no distinct `redact_customer` command
  exists (plan-r5 simplification: watermarks ride every `rebuild`/`purge`
  command, so the two compose to surgical erasure with restoration).
- All flows are idempotent under Shopify webhook redelivery (`operation_id`)
  and survive retries, races, and Redis loss: the state machine, tombstones,
  watermarks, and generation pointer live in the **engine ledger**; Wakaru's
  Redis copies are reconcilable caches refreshed by commands and envelopes.

## 5. Non-goals

- Cross-store or global learning (one merchant's data never influences
  another's simulation) — isolation is the point of per-merchant graphs.
- Replacing OASIS simulation or the report agent.
- Real-time engine reads of the store graph. The engine consumes only the
  existing `AbandonmentInsight` contract; memory changes *its quality*, not
  its shape.
- Analytics/BI on top of this data (the engine ledger is the analytical
  source; anything more is a separate product decision).

## 6. Rollout phases (each independently shippable, checkpoint-review style)

Revision 2 adds an explicit **contract gate** before Phase 1, per the
review's recommended sequence (contract → #72 → foundation → pilot → scale).

| Phase | Ships | Reads changed? | Gate to advance |
|---|---|---|---|
| 0a | #72 live (revised scheduler proven on real Redis + pinned RQ) | No | #72 steady state clean for 1 week |
| 0b | Fixed `cr-v1` ontology replaces per-event LLM ontology on the **throwaway** path, run as a sampled dual-generation experiment | No | **Ontology-sensitive gate (plan-r3 correction — the previous gate metric, insight confidence, comes from `assess_confidence_heuristic(cart)` and is independent of the ontology, so it could not fail):** (a) entity/edge-type coverage of sampled LLM-generated ontologies against `cr-v1` over ≥ 1 week, and (b) a one-time frozen paired replay of a synthetic cart corpus through both full pipelines — structured-insight agreement (`reason_category`, `recommended_angle`, `key_objections` overlap) + blinded report adjudication. Non-inferiority thresholds pre-defined in the W3 PR |
| **CG** | **Contract gate:** engine#192-**A1** (additive versioned envelope incl. `memory_generation`) + **B1** (non-exposure SQL priors) merged and deployed; envelope validated end-to-end in staging. (A2/A3 — the PII-removal steps — and B2 — exposure priors — are separately gated: A3 before any privacy claim, B2 before the pilot.) | No | Envelope fields present on real traffic |
| 1 | Dual-write: pilot merchants' cart episodes (from envelope fields) also written to their `merchant_*` graph. Analysis still reads only the throwaway graph. **Production enablement additionally requires the W4e erasure floor (verified offboard path incl. Redis-flush replay test, plus the plan-r5 purge-after-drain path; §4)** | No | V-2 exception classes pinned; readiness barrier race-tested; episodes visible via `get_by_graph_id`; erasure floor verified (offboard AND purge paths verified end-to-end) |
| 2 | Outcome ingestion (engine#192-C/D: attempts + outbox) **and lifecycle commands + rebuild feed (engine#192-E, #193)** live → outcomes land in store graphs; one full rebuild-and-verify and one redaction exercised in staging | No | Idempotent replay proven; outcome lag < 24 h; rebuild `verified_current` and redaction completion evidence observed |
| 3 | Read integration for pilot merchants: bounded working set (graph) + SQL priors (envelope) feed personas + report; throwaway graph no longer created for treated runs | **Yes** | Per-merchant instant rollback verified; latency delta < +10% |
| 4 | Decision gate on §7 → default-on for all merchants, or kill (graphs deleted, issue closed with data) | — | §7 criteria |

Flags: `STORE_MEMORY_ENABLED` (global), `STORE_MEMORY_MERCHANT_ALLOWLIST`
(comma-separated merchant UUIDs; empty = none). Sentinel-merchant traffic
(legacy engine calls without `X-Merchant-Id`) is **never** store-memory
eligible and stays on the throwaway path permanently.

## 7. Evaluation gate (revision 2 — randomized and exposure-attributed)

Pre-registered before Phase 1 starts:

- **Assignment:** eligible pilot merchants are **randomized** to treatment
  (store memory) or control (throwaway path) at enrollment; assignment is
  stable for the pilot's duration. Minimum 5 treated + 5 control merchants,
  ≥ 4 weeks in Phase 3, ≥ 50 *exposed* analyses per treated merchant.
- **Exposure-based attribution:** an analysis counts only if a
  `recovery_attempt` (engine#192-C) exists for its episode — recommendations
  that never became sends are excluded from both arms. The denominator is
  attempted recoveries, not analyses.
- **Primary metric:** recovery conversion within `ATTRIBUTION_WINDOW_DAYS`
  of the attempt (engine SQL, deterministic), treatment vs concurrent
  control. **Exposure definition (plan-r4): provider-accepted
  intention-to-treat is primary** (`accepted` is a send state — accepted
  mail can still bounce or drop); delivered-exposure, from the
  reconciliation unit's delivery states, is the pre-registered secondary. Pre-registered minimum detectable effect and a power calculation
  sized on the pilot merchants' trailing 8-week attempt volume decide
  whether the pilot can conclude at all — if power is insufficient, extend
  duration or merchant count *before* starting, not after.
- **Secondary:** insight `confidence` distribution; working-set size (D3
  bound respected); wall-clock and LLM/Zep cost per analysis (≤ +10% of
  throwaway baseline).
- **Kill criteria:** no primary-metric lift at the pre-registered threshold,
  or sustained cost/latency regression ⇒ Phase 3 reverts, treated graphs are
  deleted, issue closes with the data attached.

## 8. Success metrics (operational, ongoing after default-on)

- Per-merchant graph episode count and age histogram (D4 enforcement visible;
  rebuild cadence proven by "oldest episode age" never exceeding 180 d).
- `store_memory_working_set_size` per analysis (D3 bound respected).
- Outcome-episode ingestion lag (engine event time → episode added).
- Redactions (shop and customer) completed and verified within deadline: 100%.
