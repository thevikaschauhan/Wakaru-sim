# Implementation plan — Zep graph lifecycle (#72) + store memory (#61)

**Governing specs (revision 3, this branch):**
[issue-72/PRD](./issue-72/PRD.md) · [issue-72/TDD](./issue-72/TDD.md) · [issue-72/SCHEMA](./issue-72/SCHEMA.md) ·
[issue-61/PRD](./issue-61/PRD.md) · [issue-61/TDD](./issue-61/TDD.md) · [issue-61/SCHEMA](./issue-61/SCHEMA.md)
**Engine issues (bodies re-synchronized 2026-07-22, second pass):** [#191](https://github.com/thevikaschauhan/vakaru-engine/issues/191), [#192](https://github.com/thevikaschauhan/vakaru-engine/issues/192) (parts A1-A3/B1/B2/C1-C4/D/E), [#193](https://github.com/thevikaschauhan/vakaru-engine/issues/193)
**Status:** plan revision 3 (2026-07-22, after the second external plan
review). **Conditionally ready: E1, W1, E2a may start now; W2 after W1; W3
may start as revised below.** All other units are blocked on the gates in
their sections.

> **Plan revision 3.** Phase-0 gates repaired: G3 now measures
> ontology-sensitive signals (the r2 gate metric, insight confidence, is
> `assess_confidence_heuristic(cart)` — independent of the ontology, it
> could never fail); W4 waits for seven clean post-flip days (G0), E3a, and
> a pre-registered evaluation (P0). The episode↔send correlation is a
> four-step consumer-first propagation chain (the episode key never reaches
> Inkwell today — verified in both repos), with a **send-attempt state
> machine** replacing the impossible "atomic with the provider call" claim.
> Rebuild lifecycle gains `cleanup_pending`; `verified_current` moves to
> W6c and E6b enables only after the full consumer path. Redaction gains
> the missing Inkwell erasure unit (I3) and E7d's true fan-in. PII removal
> is an explicit three-step migration (E2a/WP1/E2b) with a wire-capture
> proof. Signing is the **complete** #73 contract (version, key id, atomic
> nonce consumption, fail-closed nonce store, idempotency-body binding,
> rotation), pinned in TDD §4.2 / SCHEMA §5.

## Ground rules

1. **One unit = one PR = one deployable invariant**, reviewed and merged
   before its dependents start. Each PR must be deployable disabled and
   independently reversible.
2. **Deployment topology matrix:**

   | Change shape | Order | Applies to |
   |---|---|---|
   | Additive fields into an existing endpoint whose consumer **ignores extras** | Producer first (verified for the Wakaru analyze payload) | E2a, E3a, E3b |
   | New request fields the **receiver must persist or strictly validates** | Receiver's tolerant handler first, then the emitter (Inkwell's event decoders are strict) | I1a→E4a, E4b→I1b |
   | **New endpoint** / new consumer | Consumer deploys dark + readiness proof → producer deploys with dispatch OFF → enable → live retry proof | W5→E5-enable, W6a/W6b/W6c→E6b-enable, I3→E7c, W6-path→E7d |
   | Field **removal** | Consumer stops requiring it → producer stops sending it → wire-capture test proves absence | WP1→E2b |

3. **Enablement protocol (every cross-repo unit):** named producer dispatch
   flag + consumer accept flag; a readiness check advertising supported
   schema versions; documented queue/backlog behavior while disabled
   (outbox rows accumulate, never dropped); a rollback owner and an abort
   threshold (error-rate or backlog-age) in the PR description.
4. **Repos, trees, tests:**
   - Wakaru: `~/Desktop/wakaru/wakaru-main` (never `~/Desktop/MiroFish-main`,
     a stale copy). Tests: `../.venv/bin/pytest` from `backend/`; lint
     `uvx ruff check app tests --select E9,F63,F7,F82`.
   - Engine: fresh worktree off `origin/main` (engine-main is parked).
     Tests: build + `go vet ./...` + DB-gated tests against a throwaway
     migrated Postgres.
   - Inkwell: `~/Desktop/vakaru-inkwell` (Go; test DB on `:55432`, tests
     gated on `INKWELL_TEST_DSN`; every new table lands its gated
     round-trip test in the same PR).
   - Check `git branch --show-current` before any commit; all three repos
     see concurrent sessions.
5. Every unit lists its **Done-when**; a unit is not done until its tests
   are green and the V-items scheduled in it are verified and recorded on
   the PR.
6. Spec deviations discovered mid-unit are owned in the PR description and
   folded back into the spec docs in the same PR.
7. **Signing:** the store-memory blueprint (W5/W6a and their engine callers)
   implements the **complete issue-#73 contract from day one** — canonical
   envelope `version | key_id | method | canonical_path | merchant_id |
   timestamp | nonce | SHA256(body)`, atomic nonce consumption, fail-closed
   nonce-store outage behavior, Idempotency-Key bound to the body hash
   (same key + different body ⇒ 409), overlapping key-id rotation with
   unknown/retired ids failing closed, constant-time comparison. Conformance
   tests in both repos: canonicalization, clock skew, nonce replay,
   nonce-store outage, tenant substitution, same-key/different-body, key
   rotation. (Full text: issue-61 TDD §4.2.) Legacy cart-recovery endpoints
   migrate separately under #73 — not a dependency of this plan.

## Gates (each produced exactly once)

| Gate | Produced by | Meaning |
|---|---|---|
| **OP1** | M1 runbook | #72 closed: backlog swept, 48 h steady-state proof posted |
| **G0** | Operator observation after OP1 | **Seven consecutive clean days** post-flip (`oldest_scratch_age_s < TTL`, `skipped_unknown_age = 0`, Sentry monitor green) — the #61 PRD Phase-0a "steady state clean for 1 week" gate |
| **G3** | W3 experiment | Ontology-sensitive non-inferiority passed (coverage + frozen paired replay; see W3) |
| **P0** | Evaluation pre-registration | Assignment rule, primary metric, MDE, power calc, exclusions, analysis method frozen **before any Phase-1 production data exists** |
| **R5** | Outcome round-trip | W5 dark + readiness ⇒ E5 dispatch enabled ⇒ live delivery + idempotent replay + 503-retry proven |
| **R6** | Lifecycle round-trip | W6a+W6b+W6c readiness ⇒ E6b dispatch enabled ⇒ staging rebuild reaches `verified_current` (incl. stale-generation re-list) |

## Dependency graph

```
M1  E1 (independent)
    W1 ──▶ W2 ──▶ OP1 ──▶ G0
M2  E2a ──▶ E3a          E2a ──▶ WP1 ──▶ E2b (PII removal proof)
M3  W3 ──▶ G3            P0 (paper gate, before W4 enablement)
    G0 + G3 + E2a + E3a + P0 ──▶ W4 (enable on pilot allowlist)
M4  C1 ──▶ I1a ──▶ E4a ──▶ E4b ──▶ I1b ──▶ E5 (dispatch OFF)
    W4 ──▶ W5 (dark) ── W5 + E5 ──▶ R5
M5  E6a ──▶ E6b-impl (dispatch OFF)
    W4 ──▶ W6a (dark) ──▶ W6b ──▶ W6c ──▶ R6 (enable E6b dispatch)
    E6a ──▶ E7a ──▶ E7b (engine leg)
    I3 (Inkwell erasure consumer, dark) ──▶ E7c (Inkwell leg)
    R6 ──▶ E7d (Wakaru leg: offboard needs W6a; redact_customer needs R6)
    E5 + sample-volume gate (N≥30/cell or seeded staging set) ──▶ E3b
M6  W4 + E3a ──▶ W7 (disabled by default)
M7  P1 ⇐ fan-in: G0 · W4 stable ≥2 wk · R5 · R6 · E7b/c/d complete · E3b · W7 rollback-tested · P0 (already frozen)
    P1 ──▶ P2 (4-week decision)
```

---

## M1 — Stop the leak (#72)

### E1 — engine: GDPR log PII fix (engine#191) — size XS
- `handlers/shopify_webhooks.go`: `logGDPRPayload` logs topic + shop + body
  **length** only; test asserts a customers/redact payload's email never
  appears in the log line.
- **Done-when:** test green; deployed.

### W1 — Wakaru: lifecycle ledger + inline delete — size S
Spec: issue-72 TDD §3.1, §3.3; SCHEMA §2.1-2.3.
- Thread `merchant_id` into `run_cart_recovery` (default sentinel); update
  both callers. `captured["graph_id"]` via `_persist_graph_id`; new
  `services/graph_lifecycle.py` (`record_created` asserting
  `graph_kind=scratch`, `record_deleted`, `created_at_for`,
  `graphs_for_merchant`; Redis-unavailable ⇒ warn + no-op). Fourth guarded
  block in `_cleanup_artifacts`.
- Tests: TDD §6 items 1-2 + ledger units; existing cleanup suite green.
- **Done-when:** staging run shows end-of-analysis deletion + ledger hash;
  #72 V-1 recorded on the PR.

### W2 — Wakaru: sweeper + scheduler + heartbeat — size M. After W1.
Spec: issue-72 TDD §3.2, §3.4; SCHEMA §2.2, §2.4.
- Sweeper (streaming, bounded heap, fail-closed proven age, dry-run single
  parser rule, `ZEP_SWEEP_FORCE_DELETE_IDS`, Zep-scan metrics, registry
  reconciliation). Scheduler (maintenance queue, `with_scheduler=True`,
  unique occurrence ids, `SET NX` lock with Lua-only release, chain marker,
  `on_failure` re-seed, boot reconciler, Sentry Cron check-in).
- Tests: TDD §6 items 3-11 + integration 12-16 (real Redis + pinned RQ; the
  three pinned regressions mandatory).
- **Done-when:** all green incl. gated integration; #72 V-2/V-3 recorded;
  staging dry sweep logs a sane inventory.

### OP1 — operator: historical sweep + #72 closure — no code. After W2.
Spec: issue-72 TDD §8. Deploy → review dry-sweep inventory (paste into #72)
→ flip `ZEP_SWEEP_DRY_RUN=false` on the worker → 48 h steady-state proof
(incl. a verified missed-check-in alert in staging) → post before/after
`total_count` → **close #72** → record the Zep DPA reference in
`docs/integration.md`.
- **Produces OP1.** **G0** then accrues: seven consecutive clean days
  post-flip, observed by the operator against the same three signals — this,
  not OP1, is what W4 waits for (#61 PRD Phase 0a).

---

## M2 — Contract gate + PII migration (engine#192-A/B1)

### E2a — engine: additive versioned envelope (#192-A1) — size M
- Adds `schema_version`, `event_id`, `merchant_id`, `shopify_store_id`,
  `anonymous_id`, `episode_type`, `checkout_started_at`, `occurred_at`,
  `memory_generation` (0 until E6a), `watermarks` (empty until E7a).
  Normative JSON Schema committed engine-side. Producer-first is safe
  (verified: Wakaru ignores unknown keys). **Adds fields only — removes no
  PII** (that is WP1+E2b).
- **Done-when:** fields visible on a real staging abandonment (log proof).

### E3a — engine: StorePriors, non-exposure stats (#192-B1) — size S. After E2a.
- Repeat-abandoner flag, `ontology_code` distribution, volume stats; SQL
  unit tests.
- **Done-when:** block on staging traffic. **Gates W4** (the #61 PRD
  contract gate is A **and** B, not the envelope alone).

### WP1 — Wakaru: stop requiring `email`/`customer_name` — size S. After E2a.
- `ShopifyCartData` and `_build_cart_from_body` make both optional; the
  seed formatter uses neutral placeholders ("a returning shopper") when
  absent; PII tests updated.
- **Done-when:** an envelope-only request (no email/name) produces a full
  analysis in staging.

### E2b — engine: remove `email`/`customer_name` from the payload — size XS. After WP1 deployed.
- Drop the fields; **wire-capture test proves absence** on the staging
  request. Only now is the PRD-D5 pseudonymization claim true.
- **Done-when:** capture artifact attached to the PR; #192-A3 checked off.

---

## M3 — Memory foundation (#61 Phases 0b + 1)

### W3 — Wakaru: fixed `cr-v1` ontology, measured by ontology-sensitive signals — size M
Spec: issue-61 TDD §3; SCHEMA §2; PRD Phase 0b (gate text updated in
plan-r3). May start day one **as revised here**.
- `services/store_ontology.py` (`CART_RECOVERY_ONTOLOGY_V1`);
  `ONTOLOGY_MODE = fixed | llm | dual_sample` (default `dual_sample` at
  rollout). In `dual_sample`, the real pipeline always uses `cr-v1`; a
  sampled fraction (default 10%) additionally generates the LLM ontology
  out-of-band and logs a **coverage row**: LLM-proposed entity/edge types
  mapped against `cr-v1` (unmatched-type rate, attribute coverage). Type
  names only — no PII in the rows. Sampled-arm failures log, never raise.
- **Frozen paired replay (one-time, offline):** a **synthetic cart corpus**
  committed to the repo (~20 carts varied by `ontology_code` scenario,
  price band, device; synthetic because Wakaru retains no real carts and
  must not start doing so for a test) run through **both** full pipelines
  once. Compare structured insights (`reason_category` agreement,
  `recommended_angle` agreement, `key_objections` overlap) + blinded
  adjudication of the two reports per cart. Bounded cost (~40 pipeline
  runs, ≈ $2, parallelizable).
- **Explicitly not a gate metric:** `insight.confidence` — it is
  `assess_confidence_heuristic(cart)` (`cart_recovery_workflow.py:224`),
  cart-only, ontology-independent; it cannot detect the change.
- **Done-when:** suite green; `dual_sample` live; replay artifacts +
  pre-defined thresholds committed.
- **Produces G3:** coverage thresholds held for ≥ 1 week of samples AND the
  frozen-set comparison non-inferior. Then `ONTOLOGY_MODE=fixed` and the
  LLM path is deleted in a follow-up chore.

### P0 — evaluation pre-registration — paper, no code. Before W4 enablement.
- Freeze in the repo (`docs/specs/issue-61/EVALUATION.md`): randomization
  rule and unit (merchant), primary metric (exposure-attributed recovery
  conversion), MDE + power calculation on trailing 8-week attempt volume,
  eligibility/exclusions, seasonality handling, analysis method. The #61
  PRD §7 requires this **before Phase-1 production data exists** — i.e.
  before W4 is enabled for any real merchant, not at P1.
- **Done-when:** EVALUATION.md merged; if power is insufficient, pilot size/
  duration adjusted **now**.

### W4 — Wakaru: provisioning + dual-write (#61 Phase 1) — size M. **Gate: G0 + G3 + E2a + E3a + P0.**
Spec: issue-61 TDD §2, §4.1; SCHEMA §1, §3, §4.1.
- `services/store_memory.py`: deterministic ids, `ensure_store_graph` with
  readiness barrier, tombstone check, envelope-authoritative generation
  resolution (fail-closed to throwaway), watermark write-barrier (empty
  until E7a). Dual-write of the §4.1 episode behind `STORE_MEMORY_ENABLED`
  + allowlist; guarded. Contracts file + strict validation. CLAUDE.md D2
  edit in this PR.
- Enablement: default off; rollback = allowlist removal; abort threshold =
  store-memory write errors > 1% of treated runs.
- Tests: TDD §9 items 1-6 + interlock item 12.
- **Done-when:** staging pilot merchant accumulates episodes; V-2 pinned;
  V-5 measured; concurrent-first-abandonment race test green.

---

## M4 — Outcomes / Track O. Four-hop consumer-first correlation chain, then outcomes

### C1 — contract: episode↔send correlation + attempt state machine — no code
- Pinned in #192-C (body updated 2026-07-22): `event_id` propagates
  Inkwell-receiver → engine-forwarder → email document → engine send
  handler; **Inkwell generates `attempt_id`** (it owns retry identity);
  the engine persists `pending` **before** the provider call, puts
  `attempt_id` in SendGrid custom args, transitions to
  `accepted | ambiguous | failed`, re-sends only from `failed`
  (possibly-succeeded discipline). No atomicity across the provider is
  claimed anywhere.
- **Done-when:** #192-C text agreed by both repo owners (it is the
  authoritative wording; this plan summarizes it).

### I1a — Inkwell: accept + persist `event_id` on cart-abandoned — size S. After C1.
- `POST /v1/events/cart_abandoned` schema gains optional `event_id`,
  stored on the email document alongside `wakaru_analysis_id`
  (receiver-first: Inkwell's decoder is strict). Gated round-trip test.
- **Done-when:** staging event with the field persists it; without it,
  unchanged.

### E4a — engine: emit `event_id` on the cart-abandoned forward — size XS. After I1a deployed.
- `services/inkwell_forwarder.go` adds the field.
- **Done-when:** staging forward shows it stored Inkwell-side.

### E4b — engine: attempt schema + send-attempt state machine — size M. After C1 (parallel with I1a/E4a).
- `recovery_attempt` migration (`attempt_id` PK-per-store, `status`
  `pending|accepted|ambiguous|failed`, `event_id`, `email_document_id`,
  `angle`, `discount_offered`, `sent_at`); `email_send.go` accepts the
  optional fields, persists `pending` pre-provider, custom-args
  `attempt_id`, transitions post-provider; same-`attempt_id` retry returns
  state, re-sends only from `failed`. Absent fields ⇒ today's behavior.
  Gated DB tests: crash-between-provider-and-commit leaves `pending`/
  `ambiguous`, never a duplicate send on retry.
- **Done-when:** staging send with hand-crafted fields walks
  `pending → accepted`; ambiguous path proven with a stubbed provider.

### I1b — Inkwell: emit attempt fields on send — size S. After E4a + E4b deployed.
- Sender loads `event_id` from the email document, generates `attempt_id`,
  sends both + `angle` + `discount_offered`; retries reuse `attempt_id`.
- **Done-when:** staging end-to-end attempt row with correct linkage.

### E5 — engine: outcome derivation + outbox, dispatch OFF (#192-D) — size M. After I1b.
- Derivation per **accepted** attempt within `ATTRIBUTION_WINDOW_DAYS`
  (`ambiguous` excluded until reconciled); outbox UNIQUE
  `(shopify_store_id, attempt_id)`; backoff ceiling < 14 d; dispatcher
  flag OFF (rows accumulate — documented disabled-state behavior).
- **Done-when:** staging rows derive correctly; dispatcher exercised only
  against a mock in tests.

### W5 — Wakaru: outcomes consumer, dark — size M. After W4 (code); independent of E5.
Spec: issue-61 TDD §4.2; SCHEMA §4.2, §5.1.
- `idempotency.py` refactor: per-scope TTL parameter (`claim_or_get` /
  `record` take `ttl_seconds`, default 86400 preserved for paid-job
  scopes) + tests (14-day claim+record, pending/replay,
  release-on-definitive-503, hold-on-ambiguous-post-write,
  **same-key/different-body-hash ⇒ 409**).
- `api/store_memory.py` blueprint with the **full #73 envelope** (ground
  rule 7) + readiness endpoint; `POST /api/store-memory/outcomes` per
  SCHEMA §5.1.
- **Done-when:** deployed dark; contract + signing conformance tests green
  in both repos.

### R5 — gate: enable outcome dispatch — operator + engine flag flip
- W5 readiness green ⇒ flip E5 dispatcher ⇒ observe live delivery,
  idempotent replay, forced 503 → retry → success. **Produces R5.**

---

## M5 — Lifecycle and retention. Full consumer path before any dispatch

### E6a — engine: operations ledger + frozen snapshot builder (#192-E) — size M
- Operations schema + `running → cleanup_pending → verified_* | failed`
  transitions; snapshot builder (180-day, watermark-filtered,
  `occurred_at`-ordered, paged, count+checksum); **explicit sub-task:**
  verify/extend engine event retention to 180 d. No dispatch.
- **Done-when:** transition tests + deterministic snapshot replay tests
  green.

### W6a — Wakaru: command/replay/status consumers, dark — size M. After W4.
- The three endpoints per SCHEMA §5.2-5.4 (statuses incl.
  `cleanup_pending`), operation idempotency, tombstones, full-#73
  conformance, readiness advertising versions. No caller exists — dark by
  definition.
- **Done-when:** contract tests green; deployed dark.

### W6b — Wakaru: rebuild application — size M. After W6a.
- Generation creation, page replay (watermark enforcement, per-page
  idempotency), count/checksum verification, atomic flip, then report
  **`cleanup_pending`** (not terminal — plan-r3 state correction).
- **Done-when:** crash/resume + mismatched-page tests green.

### W6c — Wakaru: grace deletion + terminal verification + reaper — size S-M. After W6b.
- 2 h grace deletion of prior generations, prefix re-list, **emit terminal
  `verified_current` only when stale generations are verified absent**;
  stale-generation reaper occurrence (alert-only, own Sentry monitor).
- **Done-when:** staging rebuild walks `running → cleanup_pending →
  verified_current` with the old generation verified gone; reaper
  missed-check-in alert verified.

### E6b — engine: dispatcher/poller/re-drive — size M. Implementation after E6a (parallel); **dispatch enabled only after W6a+W6b+W6c readiness (gate R6).**
- Command dispatch, status polling (treats `cleanup_pending` as
  in-progress), timeout/backoff, re-drive on `failed`;
  `memory_generation` flows into E2a's envelope from the ledger.
- **Done-when:** unknown-result retry + terminal-state tests green; one full
  staging rebuild reaches `verified_current`. **Produces R6.**

### E7a — engine: redaction persistence + state machine (#193) — size M. After E6a.
- Durable redaction rows, watermark persistence, tombstone writes,
  idempotent under Shopify redelivery. No external cascades.
- **Done-when:** redelivery + concurrent-write tests green.

### E7b — engine: engine-table erasure leg — size S-M. After E7a.
- **Done-when:** verified completion + safe re-drive tests green.

### I3 — Inkwell: erasure consumer, dark — size M. After E7a contract.
- **New unit (plan-r3): no erasure surface exists in Inkwell today**
  (verified: `internal/engineclient/gdpr.go` documents redaction as
  intentionally absent). Authenticated merchant/customer erasure endpoint,
  Inkwell-side tombstone/write barrier for redacted subjects, terminal
  completion evidence, idempotent retries; gated round-trip tests.
- **Done-when:** deployed dark; staging erasure of a seeded document set
  verified.

### E7c — engine: Inkwell cascade leg — size S. After I3 readiness.
- **Done-when:** leg completes against staging Inkwell with evidence
  recorded; re-drive safe.

### E7d — engine: Wakaru cascade leg — size S-M. **Gate: W6a (offboard) and R6 (redact_customer — it requires snapshot production, replay, flip, and cleanup, i.e. the whole rebuild path).**
- `offboard`-only mode may enable at W6a readiness (it needs only commands
  + prefix deletion); `redact_customer` enables at R6.
- **Done-when:** staging offboard → `verified_empty` (incl.
  straggler-recreation re-drive); staging customer redact → watermark
  active + pre-cutoff episodes absent from the new generation; deadline
  alert fires on an artificially stalled operation.

### E3b — engine: exposure-attributed StorePriors (#192-B2) — size S. **Gate: E5 live (R5) + sample volume.**
- Rate-by-actually-sent-angle + discount effectiveness over **accepted**
  attempts only; minimum-sample rules (no rate under N=30 per cell, pooled
  fallback). **Staging proof: a seeded integration dataset** (staging never
  reaches N=30 organically) + production shadow metrics before the pilot
  reads them. Additive envelope version bump.
- **Done-when:** seeded-set SQL tests green; shadow priors visible on
  production envelopes. **Gates P1.**

---

## M6 — Read integration (#61 Phase 3)

### W7 — Wakaru: bounded read path, disabled by default — size M. After W4 + E3a.
Spec: issue-61 TDD §5. Buildable in parallel with M4/M5; enabled only for
pilot merchants at P1.
- `StoreGraphReader.build_working_set` (all caps), `StoreGraphScanBlocked`
  guards, treated-run ReportAgent with `graph_id=None` + tools stripped,
  persona `entities_override`, treated runs skip the throwaway graph.
- Tests: TDD §9 items 5-6 + zero-outside-adapter-reads assertion; V-3
  recorded.
- **Done-when:** treated staging run produces an insight with working set +
  priors; latency delta < +10%; per-merchant rollback exercised.

---

## M7 — Pilot and decision

### P1 — pilot launch — operator. **Fan-in gate (all required):**
G0 · W4 stable ≥ 2 weeks on pilot allowlist · R5 · R6 · E7b/E7c/E7d
complete · E3b deployed · W7 enabled for pilot + rollback tested · P0
(frozen since M3 — P1 executes the registered design, it does not write it).
- Operator: allowlist env vars, Zep plan headroom check (V-4), dashboards
  for the PRD §8 metrics.

### P2 — Phase-4 gate — no code
- ≥ 4 weeks, exposure-attributed conversion vs randomized control per the
  P0 registration ⇒ default-on or kill (treated graphs deleted, #61 closed
  with data).

---

## Standing items

- **After every merged Wakaru unit:** update `CLAUDE.md` where invariants
  changed (W4 carries the D2 edit).
- **V-1** (Zep episode-delete derived-artifact semantics) stays optional;
  needed only for the opportunistic delete optimization.
- **Wakaru #73:** fully implemented on the store-memory blueprint (ground
  rule 7); the legacy-endpoint migration proceeds independently under #73.
- Per-unit execution: feed the unit's plan section + the two relevant spec
  docs into the multi-agent execute pipeline.
