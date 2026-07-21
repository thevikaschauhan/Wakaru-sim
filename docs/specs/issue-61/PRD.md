# PRD — Per-store persistent Zep knowledge graph ("store memory") with outcome feedback

**Issue:** [#61](https://github.com/thevikaschauhan/Wakaru-sim/issues/61) (P2, enhancement)
**Depends on:** #72 (must land first — per-cart graph deletion + orphan sweep; agreed in the 2026-07-11 issue comment)
**Related:** #24 (merchant binding — landed, `g.merchant_id` is request-bound), engine #33/#34/#40 migrations (outcome data)
**Companion docs:** [TDD](./TDD.md), [SCHEMA](./SCHEMA.md)
**Status:** Proposed
**Verified against:** Wakaru `origin/main` @ `a3d7a1a`; `zep-cloud==3.13.0`; vakaru-engine migrations `001,033,034,036,040`

## 1. Product thesis

Today each analysis is amnesiac: a throwaway graph holds one cart, the
simulation runs, the insight ships, everything is destroyed (#72 makes the
destruction actually happen). Nothing Vakaru learns about a store's shoppers in
January improves the email it writes in March.

**The goal of #61 is that every additional abandonment a store processes makes
the next analysis measurably better.** That requires two things, and this PRD
deliberately scopes both, because one without the other does not compound:

- **Track M (memory):** one persistent Zep graph per merchant that accumulates
  the store's abandonment history — Model B from the issue.
- **Track O (outcomes):** recovery *results* flow back into that graph. A
  memory containing only questions (carts abandoned) cannot make the system
  smarter; the compounding signal is the answers (recovered or not, by which
  angle, at what discount). The engine already holds this data
  (`orders.matched_anonymous_id`, migration 033; `abandonment_detections`
  with `episode_type`, migrations 034/040) — it is currently never fed back.

Intelligence-over-time claim, stated falsifiably: with Track M+O live, the
per-store recovery-conversion rate for treated merchants should exceed their
own pre-treatment baseline and a concurrent control cohort (§7). If it does
not, the feature is killed at the Phase-4 gate rather than kept as cost.

## 2. What memory concretely changes in the product

| Capability | Without store memory (today) | With store memory |
|---|---|---|
| Repeat abandoner | Invisible — every cart is a stranger | `Shopper` node with prior episodes; simulation personas reflect a known hesitater, insight can say "3rd abandonment, discount-resistant" |
| Store-level price sensitivity | Re-inferred from one cart | Accumulated distribution of abandonment reasons (`ontology_code` history) conditions personas and report |
| What wins carts back *here* | Unknown | `RecoveryOutcome` episodes: which angle/discount recovered similar carts at this store |
| Product-level abandonment pattern | Invisible | `Product` nodes accumulate abandonment/recovery counts across episodes |
| Email angle selection | Heuristic + single-cart LLM read | Grounded in this store's outcome history via the report agent's graph access |

## 3. Decisions on the issue's nine design questions

These are decisions, not options. Rationale in TDD; listed here for product
sign-off.

| # | Question (from #61) | Decision |
|---|---|---|
| D1 | Ontology per store, drift? | **Fixed, versioned, code-defined cart-recovery ontology** (SCHEMA §2) replaces per-event LLM ontology for store graphs. Additive evolution only; version stamped in the ledger. Removes drift, removes one LLM call per analysis. |
| D2 | Stateless invariant | **Repealed for the graph layer only**, by design and documented in CLAUDE.md as part of the implementation PR. Local project/sim/report scratch stays per-run and per-run-deleted (#24 CP2b unchanged). |
| D3 | Store-wide graph diluting a fresh cart's personas | **Never feed the whole graph to persona generation.** The pipeline builds a bounded per-run working set: this cart's entities + `graph.search` retrieval around them + store aggregates (TDD §5). `fetch_all_nodes` on a store graph is banned in the analysis path. |
| D4 | Retention/pruning | Rolling **180-day episode retention** for active merchants, enforced by a maintenance prune job; per-graph episode cap 20,000 (oldest evicted first). |
| D5 | PII at rest | Accepted deliberately for active merchants under D4's window, documented with the Zep DPA reference; shopper identifiers stored are the engine's pseudonymous `anonymous_id`, never email/name (which the seed doc already excludes from names/logs per #7/#17). |
| D6 | Zep account/plan limits | Graph count = active merchant count (plus in-flight `mirofish_*`). Operator confirms plan headroom before Phase 3 (rollout checklist). Per-graph episode count is a tracked metric. |
| D7 | Provisioning/lifecycle | **Lazy, idempotent create-on-first-abandonment** keyed `merchant_<uuid-hex-32>`; no onboarding coupling. Offboarding deletes the graph (see §4 — this corrects the issue's acceptance draft). |
| D8 | Migration of `mirofish_*` orphans | Already owned by #72's sweeper; #61 adds nothing and its `merchant_*` graphs are structurally unsweepable (`^mirofish_[0-9a-f]{16}$` filter). |
| D9 | Concurrent same-store writes | Zep episode ingestion is server-side async per graph; provisioning race resolved by idempotent get→create→get; no cross-request locking in the hot path (TDD §6). |

## 4. Correction to the issue's acceptance draft (offboarding retention)

The issue's last acceptance bullet says: *"Merchant offboarding doesn't deletes
the store graph, we will retain the data for 180 days."*

This is **overruled** in this PRD, for a verifiable external reason: Shopify's
mandatory privacy webhooks. Every Shopify app must handle `shop/redact`, which
Shopify sends ~48 hours after uninstall and which **requires** erasure of the
shop's data. The engine (the Shopify app host) receives it; a policy of keeping
the entire shop's shopper history in Zep for 180 days after uninstall cannot
comply.

Adopted policy:
- **Active merchant:** rolling 180-day episode retention (the issue's 180-day
  number is preserved here, where it is lawful and useful).
- **Uninstalled merchant:** engine forwards the redact signal; Wakaru deletes
  the store graph, ledger entries, and merchant index within the redact
  deadline. The offboarding path is a first-class API (TDD §7), exercised in
  tests, not a runbook.

## 5. Non-goals

- Cross-store or global learning (one merchant's data never influences
  another's simulation) — isolation is the point of per-merchant graphs.
- Replacing OASIS simulation or the report agent.
- Real-time engine reads of the store graph. The engine consumes only the
  existing `AbandonmentInsight` contract; memory changes *its quality*, not
  its shape.
- Building engine-side aggregation UI/analytics on this data.

## 6. Rollout phases (each independently shippable, checkpoint-review style)

| Phase | Ships | Reads changed? | Risk |
|---|---|---|---|
| 0 | #72 live; fixed ontology extracted to code (D1) behind no flag — per-event graphs simply use the fixed ontology; A/B'd against LLM ontology on insight confidence for 1 week | No | Low; also removes an LLM call → pays for itself |
| 1 | Dual-write: pilot merchants' cart episodes also written to their `merchant_*` graph. Analysis pipeline still reads only the throwaway graph | No | Zep cost for pilots only; kill switch = stop writing, sweep nothing (graphs deleted at offboarding or kill) |
| 2 | Outcome ingestion: engine outbox → `POST /api/store-memory/outcomes` → outcome episodes in store graph (Track O) | No | New internal endpoint (existing auth stack) |
| 3 | Read integration: working-set retrieval feeds personas + report for pilot merchants (Track M pays out); throwaway graph no longer created for treated runs | **Yes** | Gated per-merchant allowlist; instant per-merchant rollback to throwaway path |
| 4 | Decision gate on §7 metrics → default-on for all merchants, or kill | — | — |

Flags: `STORE_MEMORY_ENABLED` (global), `STORE_MEMORY_MERCHANT_ALLOWLIST`
(comma-separated merchant UUIDs; empty = none). Sentinel-merchant traffic
(legacy engine calls without `X-Merchant-Id`) is **never** store-memory
eligible and stays on the throwaway path permanently.

## 7. Evaluation gate (Phase 4 pass/fail, agreed before Phase 1 starts)

- Cohort: ≥ 5 pilot merchants, ≥ 4 weeks in Phase 3, ≥ 50 treated analyses
  per merchant.
- Primary: recovery-conversion rate (engine-side attribution: order with
  `matched_anonymous_id` within the attribution window after the recovery
  email) — treated period vs same merchant's trailing 8-week baseline, and vs
  control cohort over the same calendar window.
- Secondary: insight `confidence` distribution; persona-generation entity
  counts (working-set size sanity, D3); wall-clock and LLM/Zep cost per
  analysis (must not exceed +10% of throwaway baseline).
- Kill criteria are the inverse: no conversion lift and/or cost/latency
  regression ⇒ Phase 3 reverts, graphs are deleted, issue closes with data.

## 8. Success metrics (operational, ongoing after default-on)

- Per-merchant graph episode count and age histogram (D4 enforcement visible).
- `store_memory_working_set_size` per analysis (D3 bound respected).
- Outcome-episode ingestion lag (engine event time → episode added).
- Offboarding deletions completed within deadline: 100%.
