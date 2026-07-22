# Implementation plan — Zep graph lifecycle (#72) + store memory (#61)

**Governing specs (revision 3, this branch):**
[issue-72/PRD](./issue-72/PRD.md) · [issue-72/TDD](./issue-72/TDD.md) · [issue-72/SCHEMA](./issue-72/SCHEMA.md) ·
[issue-61/PRD](./issue-61/PRD.md) · [issue-61/TDD](./issue-61/TDD.md) · [issue-61/SCHEMA](./issue-61/SCHEMA.md)
**Engine issues (bodies synchronized to the r3 contract, 2026-07-22):** [#191](https://github.com/thevikaschauhan/vakaru-engine/issues/191) (PII log bug), [#192](https://github.com/thevikaschauhan/vakaru-engine/issues/192) (contract, parts A/B1/B2/C/D/E), [#193](https://github.com/thevikaschauhan/vakaru-engine/issues/193) (redaction state machine)
**Status:** plan revision 2 (2026-07-22, after external plan review). Ready to
execute under the corrected gates below; **day-one parallel set: E1, W1, W3,
E2**. Units beyond that set start only when their inbound gates are met.

> **Plan revision 2:** deployment order is now a topology matrix (the blanket
> "engine first" rule was unsafe for new endpoints); W4 gates on #72's
> operational closure and W3's completed shadow week (matching the #61 PRD's
> own phase gates); the send contract precedes E4 which precedes I1; the
> three L-sized units are split into one-invariant PRs; E3b is a formal
> unit; W5 carries the idempotency-TTL refactor; the store-memory blueprint
> adopts #73-style signing from day one; every cross-repo unit has an
> enablement/rollback row; P1's full prerequisite fan-in is explicit.

## Ground rules

1. **One unit = one PR = one deployable invariant**, reviewed and merged
   before its dependents start. Each PR must be deployable disabled and
   independently reversible.
2. **Deployment topology matrix** (replaces "engine always deploys first"):

   | Change shape | Order | Applies to |
   |---|---|---|
   | Additive fields into an **existing** endpoint | Producer first; consumer already ignores extras (verified for the analyze payload) | E2, E3a, E3b, I1-emit |
   | **New endpoint** / new consumer | Consumer deploys **dark** + readiness proof → producer deploys with **dispatch OFF** → enable dispatch → live retry proof | E5→W5, E6b→W6a, E7 legs→W6a |
   | New request fields the **receiver** must persist | Receiver's tolerant handler first, then the emitter | E4 (engine) before I1 (Inkwell) |

3. **Enablement protocol (every cross-repo unit):** named producer dispatch
   flag + consumer accept flag; a readiness endpoint/check advertising
   supported schema versions; documented queue/backlog behavior while
   disabled (outbox rows accumulate, never dropped); a rollback owner and
   an abort threshold (error-rate or backlog-age) in the PR description.
4. **Working trees:** Wakaru = `~/Desktop/wakaru/wakaru-main` (never
   `~/Desktop/MiroFish-main`, a stale copy); engine = a fresh worktree off
   `origin/main`. Check `git branch --show-current` before any commit; both
   repos see concurrent sessions.
5. Every unit lists its **Done-when**; a unit is not done until its tests
   are green (`../.venv/bin/pytest` from `backend/` for Wakaru; `go vet
   ./...` + build + gated DB tests for engine) and the V-items scheduled in
   it are verified and recorded on the PR.
6. Spec deviations discovered mid-unit are owned in the PR description and
   folded back into the spec docs in the same PR.
7. **Signing:** the new store-memory blueprint (W5/W6a endpoints and their
   engine callers) implements the **#73-style signature from day one**
   (method + canonical path + tenant + nonce + timestamp + body digest) with
   cross-repo conformance tests — both sides are new code, so there is no
   migration burden. Legacy cart-recovery endpoints migrate separately under
   Wakaru-sim#73.

## Corrected dependency graph

```
M1  E1 (independent)
    W1 ──▶ W2 ──▶ OP1 (#72 closed + 48h proof)
M2  E2 ──▶ E3a
M3  W3 ──▶ G3 (shadow week passed)
    OP1 ─┬─▶ W4          E2 ──┘
    G3  ─┘
M4  C1 (send contract in #192-C) ──▶ E4 ──▶ I1 ──▶ E5 (dispatch OFF)
    W4 ──▶ W5 (deployed dark) ─┬─▶ R5 (readiness + live retry proof
    E5 ────────────────────────┘        ⇒ enable E5 dispatch)
M5  E6a ─┬─▶ E6b (flag off until W6a readiness)
    W4 ──▶ W6a (dark) ─┘
    E6b ──▶ W6b ──▶ W6c
    E6a + W6a ──▶ E7a ──▶ E7b/c/d (one cascade leg per PR)
    E4 ──▶ E3b (after exposure-sample threshold)
M6  W4 + E3a ──▶ W7 (disabled by default)
M7  P1 ⇐ fan-in: OP1 · W4-stable · R5 · W6c · E7 legs complete · E3b · W7
    P1 ──▶ P2 (4-week decision)
```

---

## M1 — Stop the leak (#72). Highest urgency (P1, PII + cost accruing per analysis)

### E1 — engine: GDPR log PII fix (engine#191) — size XS
- `handlers/shopify_webhooks.go`: `logGDPRPayload` logs topic + shop + body
  **length** only; test asserts a customers/redact payload's email never
  appears in the log line.
- **Done-when:** test green; deployed (Railway auto-deploy on merge).

### W1 — Wakaru: lifecycle ledger + inline delete — size S
Spec: issue-72 TDD §3.1, §3.3; SCHEMA §2.1-2.3.
- Thread `merchant_id` into `run_cart_recovery` (default sentinel); update
  both callers.
- `captured["graph_id"]` via `_persist_graph_id`; new
  `services/graph_lifecycle.py` (`record_created` with `graph_kind=scratch`
  assertion, `record_deleted`, `created_at_for`, `graphs_for_merchant`;
  Redis-unavailable ⇒ warn + no-op).
- Fourth guarded block in `_cleanup_artifacts` calling `delete_graph` +
  `record_deleted(source="inline")`.
- Tests: TDD §6 items 1-2 + ledger unit tests; existing cleanup suite stays
  green.
- **Done-when:** a local run (staging Zep key) shows the graph deleted at end
  of analysis and the `zep:graph:*` hash written; #72 V-1 (delete-of-deleted
  exception class) recorded on the PR.

### W2 — Wakaru: sweeper + scheduler + heartbeat — size M. After W1.
Spec: issue-72 TDD §3.2, §3.4; SCHEMA §2.2, §2.4.
- `services/zep_graph_sweeper.py`: streaming pass, bounded heap, fail-closed
  proven-age rule (vendor `created_at` → ledger corroboration → skip+alert),
  dry-run single parser rule, `ZEP_SWEEP_FORCE_DELETE_IDS` runbook path,
  Zep-scan-derived metrics, registry reconciliation.
- `services/maintenance_queue.py` + `worker.py`: `maintenance` queue,
  `with_scheduler=True`, unique occurrence ids, `SET NX` singleton lock with
  **Lua compare-and-delete release**, chain marker, `on_failure` re-seed,
  boot reconciler, **Sentry Cron Monitor check-in** wrapping the body.
- Tests: TDD §6 items 3-11 (unit) + 12-16 (integration, real Redis + pinned
  RQ, env-gated) — the three pinned regressions are mandatory.
- **Done-when:** all tests green incl. the gated integration file run
  locally against Redis; #72 V-2/V-3 verified in staging and recorded;
  first dry sweep in staging logs a sane inventory.

### OP1 — operator: historical sweep runbook + #72 closure — no code. After W2.
Spec: issue-72 TDD §8.
1. Deploy (web + worker, same image). Sweeper is dry by default.
2. Review the first dry sweep summary (inventory incl. unknown-age list);
   paste into #72.
3. Set `ZEP_SWEEP_DRY_RUN=false` on the **worker** service; restart.
4. Confirm 48 h steady state (`oldest_scratch_age_s < TTL`,
   `skipped_unknown_age = 0`, Sentry monitor green, missed-check-in alert
   rule verified by briefly pausing the staging worker); paste before/after
   `total_count` into #72 and **close #72**.
5. Record the Zep DPA/deletion-SLA reference in `docs/integration.md`.
- **Gate produced:** `OP1 = #72 closed + 48 h proof`. **W4 must not start
  before this gate** (issue-61 PRD Phase 0a).

---

## M2 — Contract gate (engine#192-A/B1)

### E2 — engine: versioned analyze envelope (#192-A) — size M
- Extend the Wakaru enqueue payload: `schema_version`, `event_id`
  (= detections PK string), `merchant_id` (in body), `shopify_store_id`,
  `anonymous_id`, `episode_type`, `checkout_started_at`, `occurred_at`,
  `memory_generation` (0 until E6a exists), empty-for-now `watermarks`.
- Normative JSON Schema committed engine-side; additive-only rule stated.
- Topology: additive fields into an existing endpoint ⇒ producer-first is
  safe (verified: Wakaru ignores unknown keys).
- **Done-when:** staging Wakaru receives the fields on a real abandonment
  (log proof on the PR); #61 PRD "CG" gate satisfied.

### E3a — engine: StorePriors, non-exposure stats (#192-B1) — size S. After E2.
- SQL aggregates needing no attempt data: repeat-abandoner flag,
  `ontology_code` distribution, volume stats, shipped as an envelope block.
- **Done-when:** block present on staging traffic; SQL unit tests green.

---

## M3 — Memory foundation (#61 Phases 0b + 1)

### W3 — Wakaru: fixed `cr-v1` ontology as a sampled dual-run experiment — size M
Spec: issue-61 TDD §3; SCHEMA §2. Independent — day-one start.
- `services/store_ontology.py` (`CART_RECOVERY_ONTOLOGY_V1` in the exact
  `set_ontology` shape).
- **The LLM ontology path stays behind a temporary flag**
  (`ONTOLOGY_MODE = fixed | llm | dual_sample`, default `dual_sample` at
  rollout): in `dual_sample`, all runs use the fixed ontology for the real
  pipeline, and a sampled fraction (default 10%) *additionally* generates
  the LLM ontology out-of-band and records a comparison row (entity-type
  coverage vs the fixed set, downstream insight `confidence`). This is the
  runnable experiment the r1 plan lacked — nothing is compared
  before/after across time, and the paid path never depends on the extra
  call (failures in the sampled arm are logged, never raised).
- Pre-defined non-inferiority metric (in the PR before enabling): fixed-arm
  insight-confidence distribution within X of the sampled-LLM arm over ≥ 1
  week and ≥ N runs; entity coverage gaps reviewed.
- **Done-when:** suite green; staging run produces a fixed-ontology graph;
  `dual_sample` enabled in prod.
- **Gate produced:** `G3 = the shadow week completed and passed` (then the
  flag flips to `fixed` and the LLM path is deleted in a follow-up chore).
  **W4 gates on G3, not on the W3 merge.**

### W4 — Wakaru: provisioning + dual-write (#61 Phase 1) — size M. **After OP1 + G3 + E2.**
Spec: issue-61 TDD §2, §4.1; SCHEMA §1, §3, §4.1.
- `services/store_memory.py`: `store_graph_id`, `ensure_store_graph` with
  readiness barrier (ledger claim `SET NX`, `provisioning → ready`, lazy
  ontology-version upgrade), tombstone check, envelope-authoritative
  generation resolution (fail-closed to throwaway on cache-loss +
  pre-envelope requests), watermark write-barrier (empty set until E7).
- Dual-write of the §4.1 cart episode (from envelope fields) behind
  `STORE_MEMORY_ENABLED` + `STORE_MEMORY_MERCHANT_ALLOWLIST`; guarded
  (never fails the paid run).
- Contracts file `docs/specs/issue-61/contracts/cart_episode.v1.json` +
  strict validation.
- CLAUDE.md edit in this PR: the "no shared memory between cart events"
  invariant is repealed for the graph layer (spec D2).
- Enablement: flag default off; rollback = allowlist removal (next run
  reverts to throwaway); abort threshold = any store-memory write error
  rate > 1% of treated runs.
- Tests: issue-61 TDD §9 items 1-6 + #72-interlock item 12.
- **Done-when:** pilot-allowlisted staging merchant accumulates episodes
  (visible via `get_by_graph_id`); V-2 exception classes pinned; V-5
  (`lastn` ceiling) measured and recorded; concurrent-first-abandonment
  race test green.

---

## M4 — Outcomes / Track O (#61 Phase 2 data). Consumer-first topology throughout

### C1 — contract: episode↔send correlation — no code, recorded in engine#192-C
- Agree and pin the Inkwell→engine send-request extension: optional
  `event_id` (episode key) + `angle` + `discount_offered`; engine validates
  `event_id` against the authenticated merchant and persists the attempt
  **atomically with the accepted send**. `email_document_id` remains the
  SendGrid correlation key only — it never stands in for the episode.
- **Done-when:** both repos' owners sign off on the #192-C text (already
  updated 2026-07-22); schema stub committed with E4.

### E4 — engine: attempt schema + tolerant accepting handler (#192-C step 1) — size M. After C1.
- `recovery_attempt` migration + `email_send.go` accepts the optional
  fields; absent ⇒ today's behavior, present ⇒ validated + attempt row.
  Gated DB tests per house pattern. Deploys **before** Inkwell emits.
- **Done-when:** staging send with hand-crafted fields persists the row;
  sends without fields unaffected.

### I1 — Inkwell: emit attempt metadata — size S (Inkwell repo). After E4 deployed.
- Inkwell passes `event_id` + `angle` + `discount_offered` on its engine
  send call (it knows the episode context of the email it rendered).
- **Done-when:** staging send produces an attempt row end-to-end.

### E5 — engine: outcome derivation + outbox, dispatch OFF (#192-D) — size M. After I1.
- Derivation job (`recovered`/`expired` per attempt within
  `ATTRIBUTION_WINDOW_DAYS`); `wakaru_outcome_forwards` outbox (UNIQUE
  `(shopify_store_id, attempt_id)`, terminal states, backoff ceiling
  < 14 d, same-key retries on ambiguity). **Dispatcher flag OFF** — rows
  accumulate; that is the documented disabled-state behavior.
- **Done-when:** staging rows derive correctly; dispatcher exercised
  against a mock consumer in tests only.

### W5 — Wakaru: outcomes consumer, deployed dark — size M. After W4 (code); independent of E5.
Spec: issue-61 TDD §4.2; SCHEMA §4.2, §5.1.
- **Includes the shared-module refactor the r1 plan omitted:**
  `idempotency.py` gains a per-scope TTL parameter (`claim_or_get` /
  `record` accept `ttl_seconds`, default stays 86400 for the paid-job
  scopes) with tests for 14-day claim+record TTL, pending/replay,
  release-on-definitive-503, hold-on-ambiguous-post-write.
- `api/store_memory.py` blueprint with **#73-style signing** (ground rule
  7) + readiness endpoint advertising schema versions;
  `POST /api/store-memory/outcomes`: body/header merchant match ⇒ 403,
  `Idempotency-Key = attempt_id` (14-day scope), strict schema validation,
  watermark check on write.
- Tests: TDD §9 item 7 + signing conformance + auth/PII reuse.
- **Done-when:** deployed dark (reachable, zero producer traffic), contract
  tests green both repos.

### R5 — gate: enable outcome dispatch — operator + tiny engine PR
- Preconditions: W5 dark-deployed + readiness green; E5 backlog sane.
- Flip the E5 dispatcher flag; observe a live delivery, an idempotent
  replay (redeliver the same attempt), and a forced 503 → retry → success.
- **Gate produced:** `R5 = outcomes round-trip proven live`.

---

## M5 — Lifecycle and retention. One invariant per PR; consumers dark before dispatchers

### E6a — engine: operations ledger + frozen snapshot builder (#192-E) — size M
- Operations schema + state transitions; snapshot builder (180-day,
  watermark-filtered, `occurred_at`-ordered, paged with count+checksum);
  **no dispatch**. Includes the explicit sub-task: verify/extend engine
  event retention to 180 d.
- **Done-when:** DB transition tests + deterministic snapshot replay tests
  green.

### W6a — Wakaru: command/replay/status consumers, dark — size M. After W4.
- `POST /api/store-memory/commands`, `POST /api/store-memory/replay-pages`,
  `GET /api/store-memory/operations/<id>`; operation idempotency;
  tombstones; #73-style signing conformance; readiness advertises versions.
  **No caller exists yet — dark by definition.**
- **Done-when:** contract tests green; deployed dark.

### W6b — Wakaru: rebuild application — size M. After W6a.
- Generation creation via the provisioning path, page replay with watermark
  enforcement + per-page idempotency, count/checksum verification, atomic
  current flip, `verified_current`/`failed` terminal statuses. Commands
  still disabled externally (E6b flag off).
- **Done-when:** crash/resume and mismatched-page tests green (TDD §9
  item 9).

### E6b — engine: dispatcher/poller/re-drive — size M. After E6a + W6a readiness.
- Command dispatch, status polling, timeout/backoff, re-drive on `failed`;
  `memory_generation` starts flowing into E2's envelope from the ledger.
  Flag off until W6a/W6b readiness proof; then a staged staging rebuild.
- **Done-when:** unknown-result retry + terminal-state tests green; one full
  staging rebuild reaches `verified_current` (PRD Phase-2 gate, part 1).

### W6c — Wakaru: grace deletion + stale-generation reaper — size S. After W6b.
- 2 h grace deletion of prior generations inside the operation;
  reaper occurrence (alert-only, own Sentry monitor) on the W2 fabric.
  Reaper ships dry/alert-first.
- **Done-when:** old generation verified absent post-rebuild in staging;
  reaper missed-check-in alert verified.

### E7a — engine: redaction persistence + state machine (#193) — size M. After E6a.
- Durable redaction rows, watermark persistence, tombstone writes,
  idempotent under Shopify redelivery. **No external cascades yet.**
- **Done-when:** redelivery + concurrent-write tests green.

### E7b/E7c/E7d — engine: one cascade leg per PR — size S-M each. After E7a (+ W6a for the Wakaru leg).
- E7b: engine-table erasure leg. E7c: Inkwell leg. E7d: Wakaru leg
  (`offboard` / `redact_customer` commands + completion evidence). Each leg
  enabled independently, with verified completion + safe re-drive tests;
  deadline-breach alert lands with the last leg.
- **Done-when (E7d):** staging exercises offboard → `verified_empty` (incl.
  straggler-recreation) and customer redact → watermark active + pre-cutoff
  episodes absent from the new generation (PRD Phase-2 gate, part 2).

### E3b — engine: exposure-attributed StorePriors (#192-B2) — size S. After E4/E5 data.
- Recovery rate by actually-sent angle + discount effectiveness, with
  pre-defined minimum-sample rules (no rate emitted under N=30 attempts per
  cell; pooled fallback), actual-send denominators, SQL tests, additive
  envelope version bump.
- **Done-when:** priors visible on staging envelopes for a merchant with
  sufficient attempt volume; **P1 depends on this unit.**

---

## M6 — Read integration (#61 Phase 3)

### W7 — Wakaru: bounded read path, disabled by default — size M. After W4 + E3a.
Spec: issue-61 TDD §5. Buildable in parallel with M4/M5; **enabled** only
after them.
- `services/store_graph_reader.py` (`build_working_set`, all caps);
  `StoreGraphScanBlocked` guards in `zep_tools.py` + `zep_entity_reader.py`;
  treated-run ReportAgent with `graph_id=None` + tools stripped +
  `WorkingSet`/`StorePriors` context; persona `entities_override` seam;
  treated runs skip the throwaway graph (uuid-scoped processed-wait).
- Enablement: separate read flag on top of the allowlist; rollback =
  per-merchant allowlist removal, verified in staging before any prod
  enable.
- Tests: TDD §9 items 5-6 + zero-outside-adapter-reads pipeline assertion;
  V-3 (`graph.search` tuning) done here and recorded.
- **Done-when:** treated staging run produces an insight with working set +
  priors; latency delta measured < +10%; rollback exercised.

---

## M7 — Pilot and decision

### P1 — pilot launch — size S + operator. **Fan-in gate (all required):**
`OP1` (#72 closed) · W4 stable ≥ 2 weeks on pilot allowlist · `R5`
(outcomes round-trip live) · W6c (rebuild/redaction verified in staging) ·
E7 legs complete · E3b deployed · W7 enabled for pilot + rollback tested.
- Pre-register the evaluation (PRD §7): randomized assignment, minimum
  detectable effect, power calculation on trailing attempt volume; extend
  duration/merchant count *now* if underpowered.
- Operator: allowlist env vars, Zep plan headroom check (V-4), dashboards
  for the §8 metrics.

### P2 — Phase-4 gate — no code
- ≥ 4 weeks, exposure-attributed conversion vs randomized control ⇒
  default-on or kill (treated graphs deleted, #61 closed with data).

---

## Standing items

- **After every merged Wakaru unit:** update `CLAUDE.md` where invariants
  changed (W4 carries the D2 edit).
- **V-1** (episode-delete derived-artifact semantics) stays optional; only
  needed if the opportunistic delete optimization is ever wanted.
- **Wakaru #73:** the store-memory blueprint implements its signature scheme
  from day one (ground rule 7); the legacy cart-recovery endpoints migrate
  under #73 on their own schedule — that migration is *not* a dependency of
  this plan.
- Suggested next command per unit: run it through the multi-agent execute
  pipeline with this plan section + the two relevant spec docs as input.
