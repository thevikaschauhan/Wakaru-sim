# Implementation plan — Zep graph lifecycle (#72) + store memory (#61)

**Governing specs (revision 3, this branch):**
[issue-72/PRD](./issue-72/PRD.md) · [issue-72/TDD](./issue-72/TDD.md) · [issue-72/SCHEMA](./issue-72/SCHEMA.md) ·
[issue-61/PRD](./issue-61/PRD.md) · [issue-61/TDD](./issue-61/TDD.md) · [issue-61/SCHEMA](./issue-61/SCHEMA.md)
**Engine issues:** [#191](https://github.com/thevikaschauhan/vakaru-engine/issues/191) (PII log bug), [#192](https://github.com/thevikaschauhan/vakaru-engine/issues/192) (contract, parts A-E), [#193](https://github.com/thevikaschauhan/vakaru-engine/issues/193) (redaction state machine)
**Status:** ready to execute once PR #75 is approved.

## Ground rules

1. **One unit = one PR**, reviewed and merged before its dependents start
   (checkpoint delivery, not batched).
2. **Cross-repo sequence:** the engine deploys first, then the consumer PR
   lands with live proof — never both sides in one hop. New envelope/payload
   fields are additive so the engine side is always safe to deploy alone
   (Wakaru's `_build_cart_from_body` reads known keys and ignores extras;
   verified).
3. **Working trees:** Wakaru = `~/Desktop/wakaru/wakaru-main` (never
   `~/Desktop/MiroFish-main`, a stale copy); engine = a fresh worktree off
   `origin/main` (engine-main is parked). Check `git branch --show-current`
   before any commit; both repos see concurrent sessions.
4. Every unit lists its **Done-when**; a unit is not done until its tests are
   green (`../.venv/bin/pytest` from `backend/` for Wakaru; `go vet ./...` +
   build + gated DB tests for engine) and the spec's V-items scheduled in it
   are verified and recorded on the PR.
5. Spec deviations discovered mid-unit are owned in the PR description and
   folded back into the spec docs in the same PR.

## Milestone map and dependency graph

```
M1 Stop the leak (closes #72)      M2 Contract gate         M3 Memory foundation
 E1 ──────────────── (independent)  E2 ──▶ E3a               W3 ─┐
 W1 ──▶ W2 ──▶ OP1                  (E2 also gates W4)           ├─▶ W4
                                                             E2 ─┘
M4 Outcomes (Track O)               M5 Lifecycle & retention
 I1 ──▶ E4 ──▶ E5 ──▶ W5·live       E6 ──▶ E7 ──▶ W6         E3b (after E4 data)
 (W5 code lands after W4)
M6 Read integration + pilot         M7 Decision
 W7 (after W4 + E3a) ──▶ P1 ──▶     P2 (Phase-4 gate)
```

Parallel-start set on day one: **E1, W1, W3, E2** (no interdependencies).

---

## M1 — Stop the leak (#72). Independent of everything else; highest urgency (P1, PII + cost accruing per analysis)

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

### W2 — Wakaru: sweeper + scheduler + heartbeat — size M
Spec: issue-72 TDD §3.2, §3.4; SCHEMA §2.2, §2.4. Depends on W1 (ledger).
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

### OP1 — operator: historical sweep runbook — no code
Spec: issue-72 TDD §8.
1. Deploy (web + worker, same image). Sweeper is dry by default.
2. Review the first dry sweep summary (inventory incl. unknown-age list);
   paste into #72.
3. Set `ZEP_SWEEP_DRY_RUN=false` on the **worker** service; restart.
4. Confirm 48 h steady state (`oldest_scratch_age_s < TTL`,
   `skipped_unknown_age = 0`, Sentry monitor green); paste before/after
   `total_count` into #72 and **close #72**.
5. Record the Zep DPA/deletion-SLA reference in `docs/integration.md`.
6. Confirm the Sentry Crons alert rule notifies (missed check-in test:
   pause the worker briefly in staging).

---

## M2 — Contract gate (engine#192-A/B). Gates all of #61

### E2 — engine: versioned analyze envelope (#192-A) — size M
- Extend the Wakaru enqueue payload: `schema_version`, `event_id`
  (= detections PK string), `merchant_id` (in body), `shopify_store_id`,
  `anonymous_id`, `episode_type`, `checkout_started_at`, `occurred_at`,
  `memory_generation` (from the operations ledger; 0 until M5 exists),
  empty-for-now `watermarks`.
- Normative JSON Schema committed engine-side; additive-only rule stated.
- Safe to deploy before any Wakaru change (extras ignored — verified).
- **Done-when:** staging Wakaru receives the fields on a real abandonment
  (log proof on the PR); Wakaru #61 PRD "CG" gate satisfied.

### E3a — engine: StorePriors, non-exposure stats (#192-B part 1) — size S
- SQL aggregates that need no attempt data: repeat-abandoner flag for this
  `anonymous_id`, abandonment-reason (`ontology_code`) distribution, volume
  stats. Shipped as an envelope block.
- **E3b** (rate-by-actually-sent-angle, discount effectiveness) waits for E4
  data and ships as an additive envelope change later.

---

## M3 — Memory foundation (#61 Phases 0b + 1)

### W3 — Wakaru: fixed `cr-v1` ontology on the throwaway path (#61 Phase 0b) — size S/M
Spec: issue-61 TDD §3; SCHEMA §2. Independent — can start day one.
- `services/store_ontology.py` (`CART_RECOVERY_ONTOLOGY_V1` in the exact
  `set_ontology` shape); swap out `OntologyGenerator().generate()` in
  `_run_analysis` (removes one LLM call per analysis).
- One-week shadow comparison of insight-confidence distribution
  (non-inferiority, PRD Phase 0b gate) before W4 relies on it.
- **Done-when:** suite green; staging run produces a graph with the fixed
  ontology; confidence shadow-compare started.

### W4 — Wakaru: provisioning + dual-write (#61 Phase 1) — size M
Spec: issue-61 TDD §2, §4.1; SCHEMA §1, §3, §4.1. Depends on E2 + W3
(+ W1's `graph_kind` interlock).
- `services/store_memory.py`: `store_graph_id`, `ensure_store_graph` with
  readiness barrier (ledger claim `SET NX`, `provisioning → ready`, lazy
  ontology-version upgrade), tombstone check, envelope-authoritative
  generation resolution (fail-closed to throwaway on cache-loss +
  pre-envelope requests), watermark write-barrier (empty set until M5).
- Dual-write of the §4.1 cart episode (from envelope fields) behind
  `STORE_MEMORY_ENABLED` + allowlist; guarded (never fails the paid run).
- Contracts file `docs/specs/issue-61/contracts/cart_episode.v1.json` +
  strict validation.
- Tests: issue-61 TDD §9 items 1-6 + #72-interlock item 12.
- **Done-when:** pilot-allowlisted staging merchant accumulates episodes
  (visible via `get_by_graph_id`); V-2 exception classes pinned in code;
  V-5 (`lastn` ceiling) measured and recorded; race test (two concurrent
  first-abandonments) green.

---

## M4 — Outcomes / Track O (#61 Phase 2 data). Cross-repo: Inkwell → engine → Wakaru

### I1 — Inkwell: attempt metadata on send — size S (Inkwell repo)
- Inkwell passes `angle` + `discount_offered` (+ its `email_document_id`,
  already the correlation key) to the engine send call so the engine can
  persist the attempt. Additive request fields; engine tolerates absence.

### E4 — engine: durable `recovery_attempt` (#192-C) — size M. After I1.
- Attempt row on provider-accepted send linking `email_document_id` ↔
  episode key ↔ actual angle/discount ↔ `sent_at`; migration + gated DB
  tests per house pattern.

### E5 — engine: outcome derivation + outbox (#192-D) — size M. After E4.
- Derive `recovered`/`expired` per attempt within `ATTRIBUTION_WINDOW_DAYS`
  (join `orders.matched_anonymous_id`); `wakaru_outcome_forwards` outbox
  (UNIQUE `(shopify_store_id, attempt_id)`, terminal states, backoff ceiling
  < 14 d, same-key retries on ambiguity).

### W5 — Wakaru: outcomes endpoint — size S/M. Code after W4; live after E5.
Spec: issue-61 TDD §4.2; SCHEMA §4.2, §5.1.
- `api/store_memory.py` blueprint (HMAC `before_request` mirroring
  `cart_recovery_bp`), `POST /api/store-memory/outcomes`: body/header
  merchant match ⇒ 403, `Idempotency-Key = attempt_id` (14-day scope TTL),
  503-releases / post-add-ambiguity-holds, strict schema validation,
  watermark check on write.
- Tests: TDD §9 item 7 + auth/PII reuse.
- **Done-when:** staging round-trip from a real engine outbox row, replayed
  twice, lands exactly one episode; outcome lag metric visible.

---

## M5 — Lifecycle and retention (#61 rebuild/redaction). The largest tranche

### E6 — engine: operations ledger + snapshot/replay feed (#192-E) — size L
- Operations ledger table; rebuild scheduling (cadence + on-redact + on
  ontology migration); snapshot production (180-day, watermark-filtered,
  `occurred_at`-ordered, paged with count+checksum); command dispatch +
  status polling + re-drive loop; `memory_generation` flows into E2's
  envelope. Confirm/extend engine event retention to 180 d (explicit
  sub-task — verify what the events table actually retains today).

### E7 — engine: redaction state machine (#193) — size L. After E6 skeleton.
- Durable redaction rows for shop/customers redact; watermark persistence;
  tombstone; cascade legs (engine tables → Inkwell → Wakaru commands) with
  per-leg verified completion, idempotent under Shopify redelivery;
  deadline-breach alert. Replaces the stub handlers (E1 already fixed their
  logging).

### W6 — Wakaru: commands + replay + rebuild + reaper — size L. After W4; contract from E6.
Spec: issue-61 TDD §7; SCHEMA §5.2-5.4, §3.
- `POST /api/store-memory/commands` (`rebuild`/`redact_customer`/`offboard`,
  operation idempotency, tombstone-first offboard, all-generation prefix
  enumeration, verified terminal statuses), `POST
  /api/store-memory/replay-pages` (per-page idempotency, watermark
  enforcement, count/checksum verification), `GET
  /api/store-memory/operations/<id>`.
- Generation flip + 2 h grace deletion; stale-generation reaper occurrence
  (alert-only, own Sentry monitor) on the W2 scheduling fabric.
- Tests: TDD §9 items 8-11; integration: one full rebuild-and-verify and one
  redaction crash-resume in staging (PRD Phase-2 gate).
- **Done-when:** staging exercises rebuild → `verified_current`, offboard →
  `verified_empty` (incl. straggler-recreation case), customer redact →
  watermark active + shopper's pre-cutoff episodes absent from the new
  generation.

---

## M6 — Read integration + pilot (#61 Phase 3)

### W7 — Wakaru: bounded read path — size M. After W4 + E3a.
Spec: issue-61 TDD §5. Can be built in parallel with M4/M5; **enabled** only
after them.
- `services/store_graph_reader.py` (`build_working_set`, all caps);
  `StoreGraphScanBlocked` guards in `zep_tools.py` + `zep_entity_reader.py`;
  treated-run ReportAgent with `graph_id=None` + tools stripped +
  `WorkingSet`/`StorePriors` context; persona `entities_override` seam;
  treated runs skip the throwaway graph (uuid-scoped processed-wait).
- Tests: TDD §9 items 5-6 + the zero-outside-adapter-reads pipeline
  assertion; V-3 (`graph.search` tuning) done here and recorded.
- **Done-when:** treated staging run produces an insight with working set +
  priors, latency delta measured < +10%, per-merchant rollback verified.

### P1 — pilot launch — size S + operator
- Pre-register the evaluation (PRD §7): randomized assignment, minimum
  detectable effect, power calc on trailing attempt volume; extend duration/
  merchant count *now* if underpowered.
- Operator: allowlist env vars, Zep plan headroom check (V-4), dashboards
  for the §8 metrics.

### P2 — Phase-4 gate — no code
- ≥ 4 weeks, exposure-attributed conversion vs randomized control ⇒
  default-on or kill (treated graphs deleted, #61 closed with data).

---

## Standing items

- **After every merged Wakaru unit:** update `CLAUDE.md` where invariants
  changed (W4 repeals the "no shared memory between cart events" line for
  the graph layer — spec D2 requires this edit in the W4 PR).
- **V-1** (episode-delete derived-artifact semantics) stays optional; only
  needed if the opportunistic delete optimization is ever wanted.
- **Wakaru #73** (tenant+nonce in signatures) is a strengthening that slots
  in anywhere; the new endpoints adopt it verbatim when it lands.
- Suggested next command per unit: run it through the multi-agent execute
  pipeline with this plan section + the two relevant spec docs as input.
