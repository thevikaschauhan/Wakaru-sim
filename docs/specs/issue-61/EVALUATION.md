# Evaluation pre-registration - #61 store-memory pilot (P0)

**Issue:** thevikaschauhan/Wakaru-sim#61
**Plan unit:** P0 (IMPLEMENTATION_PLAN.md, M3). Merging this file produces gate **P0**.
**Companion specs:** issue-61 PRD §7 (evaluation gate) and §8 (metrics); vakaru-engine#192 parts C/D (send-attempt state machine, outcome derivation); plan units E4b/E4c/E3b. The PRD/TDD/SCHEMA live on the PR #75 branch; this document stands alone and merges independently of them.
**Status:** FROZEN at merge, before any Phase-1 production data exists. Changes only per the amendment protocol in §9.

## 1. Purpose and freeze discipline

The #61 PRD §7 requires the pilot evaluation to be pre-registered before
Phase-1 production data exists, i.e. before W4 is enabled for any real
merchant. This document is that registration. It fixes, now:

1. the randomization rule and unit (§2),
2. the primary and secondary metrics, exactly, in terms of the
   `recovery_attempt` send-state machine (§3),
3. the minimum detectable effect (MDE), the power formula, and a
   mechanical rule that maps measured attempt volume to required pilot
   size and duration (§4),
4. eligibility, exclusions, and mid-pilot churn handling (§2.2, §2.5),
5. seasonality handling (§5), and
6. the analysis method, alpha, and sidedness (§6).

Once the operator fills in the measured inputs (§7), zero discretionary
choices remain. If measured power is insufficient, pilot size and duration
adjust **now**, per the ladder in §4.5, not after data exists. P1 executes
this design; it does not write or revise it.

## 2. Randomization

### 2.1 Unit

The unit of randomization is the **merchant** (engine merchant UUID, the
canonical lowercase hyphenated form stored in the engine ledger). All
inference is clustered at the merchant level (§6); no attempt-level test is
run. Assignment is fixed at enrollment and stable for the pilot's duration
(PRD §7).

### 2.2 Eligibility

A merchant is eligible if, at the enrollment snapshot (§2.3), all of the
following hold:

1. The Vakaru app is installed and abandonment detection is active.
2. The install date is at or before snapshot minus 56 days (a full
   baseline window exists).
3. Baseline send volume: at least 100 provider-accepted recovery sends in
   the trailing 56 days (roster A). A fallback roster B at a floor of 50
   accepted sends per 56 days exists only for the sizing ladder (§4.5) and
   carries a higher minimum duration. The roster A floor (12.5 accepted
   sends per week) is what the PRD §7 floor of "at least 50 exposed
   analyses per treated merchant" implies at the 4-week minimum duration;
   roster B satisfies it via its 8-week minimum.
4. The merchant is not the sentinel merchant (legacy traffic without
   `X-Merchant-Id` is never store-memory eligible, PRD §6), is not
   tombstoned or offboarded, and is not a development or test store
   operated by Vakaru itself (any such stores are listed explicitly in the
   roster commit).

Merchants that install after the snapshot (**late joiners**) are not
enrolled and never enter the analysis. They stay on the throwaway path. A
later enrollment wave requires a new pre-registration amendment merged
before any of that wave's data is used.

### 2.3 Enrollment snapshot and roster commit

The snapshot instant is **00:00 UTC on the Monday of the ISO week in which
the W4 pilot allowlist is first populated with any real merchant**. The
roster commit must merge before that flag flips, and records: the snapshot
timestamp, the exact SQL run (§4.3), per-merchant inputs
(`accepted_56d`, `episodes_56d`, `b_i`, `v_i`), the full HMAC digest per
merchant, the pairing, and the resulting arms. Anyone can recompute the
assignment from the roster commit; a mismatch voids the registration (§9).

### 2.4 Assignment mechanism (deterministic, auditable, not re-rollable)

1. Compute each eligible merchant's baseline conversion `b_i` and weekly
   accepted-send volume `v_i` from the frozen queries (§4.3).
2. Sort the roster by `b_i` descending; break ties by merchant UUID
   ascending.
3. Form consecutive pairs (1,2), (3,4), ... If the roster count is odd,
   the last merchant (lowest `b_i`) is not enrolled.
4. For each merchant compute
   `t_i = HMAC-SHA256(key, lowercase merchant UUID string)`, hex digest,
   with the frozen key `wakaru-61-p0-evaluation-2026-07-22`.
5. Within each pair, the merchant with the lexicographically smaller hex
   digest is assigned **treatment** (store memory); the other **control**
   (throwaway path).

Why this cannot be gamed or re-rolled:

- The key, the mechanism, and the eligibility criteria are frozen in this
  commit, in git history, before any roster exists.
- Merchant UUIDs are engine-minted at install time, before the pilot
  existed; no party chooses them.
- The snapshot instant is fixed by an objective event (§2.3), not chosen
  after inspecting digests.
- `b_i` and `v_i` come from frozen queries over historical data; the roster
  commit publishes them, so the sort order is auditable.
- Assignment is a pure function of the committed roster. Re-running it is
  the audit. There is no randomness to re-roll.

The W4 allowlist and the Phase-3 (W7) allowlist must equal the treatment
arm exactly. The operator asserts this equality at each enablement and
weekly during the pilot; an uncorrected mismatch is a protocol breach (§9).

### 2.5 Mid-pilot churn, uninstall, and redaction

- **Intention-to-treat at the merchant level:** once enrolled, a merchant
  stays in its assigned arm for analysis regardless of what happens later.
  Treated runs that fall back to the throwaway path (errors, caps,
  rollback) remain treated. No post-randomization exclusion may be based
  on any outcome-correlated quantity.
- **Weekly aggregate snapshots:** during the pilot the operator runs the
  registered aggregate query weekly and commits per-merchant cumulative
  `{accepted, recovered}` counts by arm. These snapshots are the fallback
  record if raw ledger rows are later erased by privacy webhooks.
- **Uninstall (`shop/redact`):** the merchant remains in ITT with the
  attempts accrued before erasure; the final analysis uses the ledger
  where available and the latest committed snapshot otherwise. Churn
  counts are reported by arm (differential churn is itself a treatment
  effect signal and must be visible).
- **`customers/redact`:** attempts of the redacted shopper already
  captured in a committed snapshot stay in the aggregates; the count of
  redaction-affected attempts is reported per arm. Expected to be rare.
- **Zero-attempt merchants:** a pair in which either member has zero
  accepted attempts in the pilot window is excluded whole (both members).
  This rule is volume-based and outcome-blind; excluded-pair counts are
  reported by arm.

## 3. Metrics

### 3.1 Send-state definitions (from engine#192-C, plan units E4b/E4c)

`recovery_attempt.status` (send state): `pending -> accepted | ambiguous |
failed`.

- `accepted`: the provider accepted the send request, either directly
  (SendGrid API success at send time) or by E4c reconciliation: any
  authenticated provider webhook carrying the attempt's `attempt_id`
  (`processed`, `delivered`, `deferred`, `dropped`, `bounce`) proves API
  acceptance and monotonically reconciles `pending`/`ambiguous` to
  `accepted`.
- `failed`: **pre-acceptance positive rejection only** (the provider API
  rejected the send request). Delivery outcomes never map to it.
- `delivery_state` (`none | processed | delivered | dropped | bounced`) is
  tracked separately, never overwrites send state, and `dropped`/`bounced`
  never make an attempt retryable.

### 3.2 Primary metric: provider-accepted intention-to-treat (ITT)

- **Exposure (denominator):** every `recovery_attempt` with
  `status = accepted` and `sent_at` inside the pilot window, belonging to
  an enrolled merchant, counted in that merchant's assigned arm.
  - `accepted` is a send state, not a delivery state: an accepted attempt
    that later shows `delivery_state` of `dropped` or `bounced` **stays in
    the denominator**. That is the intention-to-treat property at the
    message level and this registration's primary exposure definition
    (plan r4, H2; frozen here, not decided after data exists).
  - `failed` attempts are excluded from both arms: no send occurred.
  - Attempts still `pending` or `ambiguous` at the analysis instant are
    excluded from both arms until reconciled by E4c; their count is
    reported per arm and is expected to be near zero (webhook
    reconciliation plus the 24-hour stale-attempt watchdog).
- **Outcome (numerator):** the attempt is `recovered` within
  `ATTRIBUTION_WINDOW_DAYS` (default 7; frozen at 7 for this pilot) of
  `sent_at`, per the engine#192-D derivation: an order on the same store
  whose `matched_anonymous_id` equals the attempt's episode
  `anonymous_id` (joined through the attempt's `event_id`), with order
  `created_at` in `(sent_at, sent_at + 7 days]`. Deterministic engine SQL;
  never derived from graph retrieval.
- **Per-merchant summary:** `p_i = recovered / accepted` over the pilot
  window. The primary estimand is the difference in mean per-merchant
  conversion (equal merchant weights), treatment minus control, over
  analyzable pairs.
- Both arms are measured identically: sending is arm-independent (every
  abandonment gets the recovery flow); only the analysis feeding the email
  content differs. Attempt volume per merchant-week by arm is reported as
  a balance check.

**Pilot window:** starts at the recorded W7 enablement timestamp `T3`
(committed in the Phase-3 sizing commit, §4.5) and spans `w` consecutive
7-day periods; exposure counts attempts with `sent_at` in
`[T3, T3 + 7w days)`. The single analysis runs at
`T3 + (7w + 10) days`: the window end plus the 7-day attribution window
plus a 3-day reconciliation grace period. No interim efficacy analysis.

### 3.3 Secondary metric: delivered-exposure (pre-registered as secondary only)

Same construction as §3.2 with the denominator restricted to accepted
attempts whose `delivery_state = delivered` at the analysis instant (E4c
delivery states). Reported with the same test, explicitly
non-confirmatory: delivery is post-randomization and plausibly
treatment-influenced (content affects drops and bounces), so this is a
per-protocol-style estimate subject to selection bias. **It cannot
overturn, rescue, or replace the primary result under any outcome.**

### 3.4 Guardrails (operational, not hypothesis tests)

From PRD §7: wall-clock and LLM/Zep cost per treated analysis at most +10%
of the concurrent throwaway baseline; working-set size within the D3
bound. A sustained guardrail breach kills the pilot regardless of the
primary result (PRD kill criteria). Guardrail readings are reported
descriptively.

## 4. MDE and power

### 4.1 Frozen statistical parameters

| Parameter | Value | Rationale |
|---|---|---|
| alpha | 0.05, one-sided | The decision is asymmetric: default-on requires demonstrated lift; both "no effect" and "harm" kill the feature (PRD §7), so the type-I error of interest is one-directional |
| Power | 0.80 | Standard |
| MDE | Relative lift of 1.5x on baseline conversion, with a frozen fallback ladder to 1.75x and 2.0x (§4.5) | Store memory must be decisively better to justify permanent PII retention and cost; small lifts do not clear the product bar |
| `rho_pair` | 0.01 | Residual merchant-level intra-class correlation after pairing on baseline conversion. Assumes raw between-merchant ICC near 0.02 and that matching absorbs about half. An assumption, frozen; the ladder is applied with this value regardless |
| Duration floor / cap | 4 weeks (PRD §7) / 12 weeks | The cap bounds decision latency; a pilot that cannot conclude within a quarter does not launch as confirmatory (§4.5, step L7) |
| Pairs floor | J >= 5 | The pair-flip permutation test's smallest attainable one-sided p is 2^-J; J = 5 gives 1/32 = 0.03125 < 0.05. Also the PRD floor of 5 treated + 5 control |

### 4.2 Power formula

The analysis (§6) compares merchant-level conversion within matched pairs.
For `J` pairs, per-merchant pilot attempt count `m = v * w` (weekly
accepted volume `v`, duration `w` weeks), true rates `p0` (control) and
`p1` (treatment):

```
V     = (p0*(1-p0) + p1*(1-p1)) / 2          # mean binomial variance
delta = p1 - p0
B     = 2 * (z_alpha + z_power)^2 * V / delta^2
      = 12.365 * V / delta^2                 # z_0.95 = 1.6449, z_0.80 = 0.8416

Required pairs:   J >= B * (rho_pair + 1/(v*w))
Required weeks:   w  = 1 / (v * (J/B - rho_pair))   [defined only if J > B*rho_pair]
```

Derivation: the variance of one within-pair difference of merchant
conversion rates is approximately `2*V*(rho_pair + 1/m)`; the test
statistic is the mean of `J` such differences; the normal-approximation
power condition `delta >= (z_alpha + z_power) * sqrt(2*V*(rho_pair + 1/m) / J)`
solves to the expressions above. `B` is the familiar unclustered
two-proportion per-arm sample size; `J > B * rho_pair` is the hard floor on
pairs that no duration can buy back: between-merchant variance, not
attempt count, is the binding constraint.

The formula is evaluated with `v = v_bar`, the **median** of enrolled
merchants' weekly accepted volumes (for an even count, the lower middle
value; conservative), rounded down to one decimal. `p0` is the pooled
baseline conversion over the roster (total recovered / total denominator,
§4.3), rounded to four decimals. `p1 = MDE * p0`.

### 4.3 Operator measurement queries (the number this document waits for)

The real trailing volume requires production queries the operator must
run at the snapshot instant (§2.3). Semantics are normative; the SQL below
is the template (column names per engine migrations 033/034/040); the
roster commit records the SQL actually run.

**Q1 - per-merchant accepted-send volume, trailing 56 days.** Canonical
once E4b is live:

```sql
SELECT shopify_store_id, COUNT(*) AS accepted_56d
FROM recovery_attempt
WHERE status = 'accepted'
  AND sent_at >= :snapshot - INTERVAL '56 days'
  AND sent_at <  :snapshot
GROUP BY shopify_store_id;
```

Until `recovery_attempt` holds 56 days of history, the proxy is
provider-accepted recovery-email sends over the same window from the send
pipeline's persisted records (Inkwell send spine; a send counts iff the
SendGrid API call returned success). `v_i = accepted_56d / 8` (per week).

**Q2 - per-merchant baseline conversion, trailing 56 days.** Episode-level
proxy until Q1's canonical source has history; the same construct is used
for every merchant symmetrically, and it feeds only pairing and power
inputs, never the confirmatory contrast:

```sql
SELECT d.shopify_store_id,
       COUNT(*) AS episodes_56d,
       COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM orders o
         WHERE o.shopify_store_id     = d.shopify_store_id
           AND o.matched_anonymous_id = d.anonymous_id
           AND o.created_at >  d.checkout_started_at
           AND o.created_at <= d.checkout_started_at + INTERVAL '7 days'
       )) AS recovered_56d
FROM abandonment_detections d
WHERE d.checkout_started_at >= :snapshot - INTERVAL '56 days'
  AND d.checkout_started_at <  :snapshot
GROUP BY d.shopify_store_id;
```

`b_i = recovered_56d / episodes_56d`; pooled
`p0 = sum(recovered_56d) / sum(episodes_56d)` over the roster. Once
`recovery_attempt` predates the snapshot by 56 days, the attempt-based
version (denominator = accepted attempts, numerator = recovered per §3.2)
replaces the episode proxy; the roster commit states which construct was
used.

### 4.4 Sensitivity table

Scale of the problem by baseline rate (MDE 1.5x, unclustered per-arm
attempt requirement `B`, and the minimum pairs `J_min` that no duration can
reduce, at `rho_pair = 0.01`):

| p0 | p1 | B (attempts per arm) | J_min (pairs) |
|---|---|---|---|
| 0.02 | 0.030 | 3011 | 31 |
| 0.03 | 0.045 | 1981 | 20 |
| 0.05 | 0.075 | 1157 | 12 |
| 0.08 | 0.120 | 693 | 7 |

Required duration in weeks at `p0 = 0.05` (illustrative center), by MDE
tier, pairs `J`, and median weekly accepted volume per merchant `v_bar`.
Cells apply `w = max(4, ceil(w_req))`; `>12` means the cap is exceeded and
the ladder moves on. Computed from the §4.2 formula with
`rho_pair = 0.01`; constants `B`: 1156.2 (1.5x), 559.9 (1.75x), 340.0
(2.0x).

**MDE 1.5x (p1 = 0.075), J_min = 12:**

| Pairs J | v_bar = 10/wk | v_bar = 25/wk | v_bar = 50/wk |
|---|---|---|---|
| 12 | >12 | >12 | >12 |
| 15 | >12 | >12 | 7 |
| 20 | >12 | 6 | 4 |
| 25 | 9 | 4 | 4 |

**MDE 1.75x (p1 = 0.0875), J_min = 6:**

| Pairs J | v_bar = 10/wk | v_bar = 25/wk | v_bar = 50/wk |
|---|---|---|---|
| 6 | >12 | >12 | >12 |
| 8 | >12 | 10 | 5 |
| 10 | >12 | 6 | 4 |
| 12 | 9 | 4 | 4 |

**MDE 2.0x (p1 = 0.10), J_min = 4 (test floor J >= 5 binds):**

| Pairs J | v_bar = 10/wk | v_bar = 25/wk | v_bar = 50/wk |
|---|---|---|---|
| 5 | >12 | 9 | 5 |
| 6 | >12 | 6 | 4 |
| 8 | 8 | 4 | 4 |

Honest reading: at the `J_min` row of each tier the pilot is never
feasible within the cap; merchant count, not duration, is the lever. The
PRD's 5+5 floor supports only the 2.0x tier at moderate-to-high volume.

### 4.5 Mechanical sizing ladder (zero post-hoc discretion)

Inputs: roster A and roster B (§2.2 volume floors), their sizes `K_A`,
`K_B`, pooled `p0`, and `v_bar`, all from §4.3. `J = floor(K/2)` for the
roster in use. Evaluate the steps in order; **the first satisfiable step
launches with its tier and duration**:

| Step | MDE | Roster (floor per 56 d) | Week floor | Condition |
|---|---|---|---|---|
| L1 | 1.5x | A (>= 100) | 4 | J >= max(5, J_min) and w <= 12 |
| L2 | 1.5x | B (>= 50) | 8 | same |
| L3 | 1.75x | A | 4 | same |
| L4 | 1.75x | B | 8 | same |
| L5 | 2.0x | A | 4 | same |
| L6 | 2.0x | B | 8 | same |
| L7 | none | - | - | Do not launch as a confirmatory pilot |

where `w = max(week floor, ceil(w_req))` per §4.2 and
`J_min = floor(B * rho_pair) + 1`. Roster B's 8-week floor preserves the
PRD's 50-exposed-per-merchant floor at its lower volume bound.

**If measured power is insufficient at a step, the ladder IS the
adjustment: size (roster) and duration (w) move now, before any pilot data
exists, exactly as the plan requires.** At L7 the pilot cannot reach 80%
power to detect even a doubling within 12 weeks; Phase 3 must not launch
as a confirmatory pilot, and #61 returns to the plan owner with this
document's computed inputs attached. Any non-confirmatory launch requires
an amendment commit, merged before Phase-3 enablement, stating the
achieved power at MDE 2.0x.

**Two evaluations, both committed, both mechanical:** the ladder runs once
at enrollment (fixes roster, pairs, and J) and once at Phase-3 enablement
(re-runs the same formula on the enrolled roster's then-current trailing
56-day volume to fix the tier and `w`; pairing and J never change). The
second run is blinded sample-size re-estimation on pre-exposure data: no
treatment-vs-control outcome exists before Phase 3, because reads are off
until W7 flips. Its output is committed (the Phase-3 sizing commit,
recording `T3`, `v_bar`, the chosen step, and `w`) before the W7 flag
flips.

## 5. Seasonality

- The primary defense is the design: a concurrent randomized control
  measured over the identical calendar window, so common seasonal shocks
  (promotions, holidays, BFCM) cancel in the arm contrast.
- No calendar weeks are excluded or reweighted, whatever they contain.
  Frozen: there is no discretionary "unusual week" carve-out.
- Pairing on baseline conversion gives partial balance on merchant type
  and seasonal sensitivity; it affects efficiency, not validity.
- Baseline `b_i` is a pairing and power input only, never a counterfactual.
  No before/after claim is registered, so trailing-window seasonality
  mismatch cannot bias the primary contrast.
- Weekly attempt volume by arm is reported so any seasonal swing is
  visible in the read-out.

## 6. Analysis method

- **Test:** exact permutation test for a matched-pair cluster-randomized
  design. Statistic `T = (1/J') * sum_j (p_T,j - p_C,j)` over the `J'`
  analyzable pairs (§2.5), where `p_i` is the §3.2 per-merchant
  conversion. The permutation distribution flips treatment labels
  independently within each pair (the actual randomization scheme),
  enumerating all `2^J'` arrangements when `J' <= 20`; otherwise Monte
  Carlo with 100,000 draws, RNG seed 61, p-value
  `(1 + #{T* >= T_obs}) / (N + 1)`. Exhaustive p-value:
  `#{T* >= T_obs} / 2^J'`, identity included.
- **Alpha and sidedness:** one-sided, alpha = 0.05, H1: treatment exceeds
  control (rationale in §4.1).
- **How clustering is respected:** the merchant is the unit of analysis
  and of permutation. Attempts never enter a test directly; they only form
  per-merchant summaries. No attempt-level model, no attempt-level
  standard errors.
- **Estimate reported:** `T_obs` (mean within-pair difference in
  conversion), plus descriptive pooled attempt-weighted rates per arm.
- **Missing data:** per §2.5 (pair exclusion on zero attempts, snapshots
  for erased rows, unreconciled attempts excluded and counted). If
  exclusions leave `J' < 5`, p < 0.05 is unattainable; the pilot then
  concludes without demonstrated lift and the PRD kill criterion applies.
  If `J'` falls below the powered J, the test still runs as registered and
  the power shortfall is reported; there is no post-data extension.
- **Multiplicity:** exactly one confirmatory test (the primary). The
  secondary (§3.3) and all guardrails are descriptive.
- **Decision rule (executed at P2, the Phase-4 gate):** the pilot passes
  iff the primary one-sided p < 0.05 and no sustained guardrail breach
  occurred. Otherwise the feature is killed per PRD §7: Phase 3 reverts,
  treated graphs are deleted, and #61 closes with the data attached. The
  registered duration `w` runs to completion; there is no early efficacy
  stop. Operational aborts (safety, cost, rollback thresholds) may stop
  the pilot at any time and count as a kill.
- **Tooling:** the analysis script implementing this section is committed
  to the repo before the pilot end date and run once at the §3.2 analysis
  instant.

## 7. Operator fill-in (to be completed in the roster and sizing commits)

Enrollment (roster commit, merges before the W4 allowlist is populated):

| Input | Value |
|---|---|
| Snapshot timestamp (§2.3) | (fill) |
| SQL actually run for Q1/Q2 | (fill) |
| K_A, K_B | (fill) |
| Pooled p0 | (fill) |
| v_bar (median, roster in use) | (fill) |
| Ladder step selected | (fill) |
| J (pairs) | (fill) |
| Excluded test stores | (fill) |

Per-merchant roster table: merchant UUID, `accepted_56d`, `episodes_56d`,
`b_i`, `v_i`, pair index, full HMAC digest, arm.

Phase-3 sizing commit (merges before the W7 flag flips): `T3`, re-measured
`v_bar`, ladder step, final `w`, and the allowlist-equality assertion
(§2.4).

## 8. Relationship to other gates

P0 is a paper gate produced by this merge. It joins G0, G3, E2a, E3a, and
W4E in gating W4's production enablement, and it is a named input to P1
(which executes the design frozen here). The metric definitions in §3
depend on E4b (attempt schema), E4c (reconciliation and delivery states),
and engine#192-D (outcome derivation) being live before Phase 3; those are
already sequenced ahead of the pilot by the implementation plan.

## 9. Registration integrity

The confirmatory status of the pilot is void if any of the following
happens without a pre-data amendment commit (merged before Phase-3
enablement):

1. The assignment recomputation from the roster commit does not reproduce
   the committed arms.
2. The roster, pairing, or arms change after the roster commit.
3. The W4/W7 allowlist diverges from the treatment arm and is not
   corrected within one week (the affected pair is dropped either way).
4. `ATTRIBUTION_WINDOW_DAYS` is changed from 7 mid-pilot.
5. The analysis deviates from §6, the metrics from §3, or the window from
   the committed `T3` and `w`.
6. Any week of pilot data is excluded or reweighted.
7. The Q1/Q2 semantics are altered after the roster commit (the SQL text
   may only be adapted to schema naming, with the semantics of §4.3
   preserved and the change recorded).

Amendments before Phase-3 enablement are permitted as visible commits to
this file; each must state what changed and why. After Phase-3 enablement,
nothing in this document changes.
