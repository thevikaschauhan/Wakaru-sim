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
   operated by Vakaru itself. The excluded-store list is frozen **here**,
   not deferred to the roster commit: freezing it before any roster exists
   is what stops it from becoming a roster-composition lever. (The
   assignment seed is independently unknowable until the post-roster beacon
   reveal, §2.4, so the list cannot be tuned against a known split either.)
   The frozen list, keyed by `shopify_store_id` with the store domain as an
   integrity check:

   | shopify_store_id | domain |
   |---|---|
   | 1 | vakaru-test.myshopify.com |
   | 3 | vakarutest1.myshopify.com |
   | 4 | vakarutest2.myshopify.com |
   | 14 | vakarutest3.myshopify.com |
   | 19 | label-house-test.myshopify.com |

   The roster commit records only the assertion that no store was added
   to or removed from this list.

Merchants that install after the snapshot (**late joiners**) are not
enrolled and never enter the analysis. They stay on the throwaway path. A
later enrollment wave requires a new pre-registration amendment merged
before any of that wave's data is used.

### 2.3 Enrollment snapshot and roster commit

The snapshot instant is **00:00 UTC on the Monday of the ISO week in which
the W4 pilot allowlist is first populated with any real merchant**.
Assignment is a two-commit protocol so that the randomizing entropy does
not exist when the roster and every input is chosen (§2.4):

1. **Roster commit** (merges before the flag flips). Records the snapshot
   timestamp, the intended flip ISO week, the exact SQL run (§4.3),
   per-merchant inputs (`accepted_56d`, `recovered_56d`, `b_i`, `v_i`), the
   deterministic pairing (§2.4 steps 1-3), and the **named future beacon
   round** that will supply the seed. It does **not** and cannot record the
   arms: the seed is not yet knowable. It publishes `SHA256` of the sorted
   roster UUID list so the roster is tamper-evident before the beacon
   emits.
2. **Assignment commit** (merges after the beacon reveal, before any
   arm-labeled data is used). Records the revealed beacon value, the seed
   derived from it, the per-merchant digest, and the resulting arms. Anyone
   recomputes the arms from (roster commit + public beacon value); a
   mismatch voids the registration (§9).

**Slippage rule (frozen):** the roster commit names both the flip ISO week
and the beacon round, and the beacon round must fall inside that week after
the snapshot. If the allowlist is not first populated within the named
week, the roster is void: its beacon round may not be reused for a
different roster, and enrollment requires a fresh roster commit (new
snapshot, new future beacon round) in an amendment commit recording the
reason for the slip. A seed is therefore bound to exactly one roster;
deferring the flip can never silently redraw the arms.

### 2.4 Assignment mechanism (deterministic, auditable, not re-rollable)

1. Compute each eligible merchant's baseline conversion `b_i` and weekly
   accepted-send volume `v_i` from the frozen queries (§4.3).
2. Sort the roster by `b_i` descending; break ties by merchant UUID
   ascending.
3. Form consecutive pairs (1,2), (3,4), ... If the roster count is odd,
   the last merchant (lowest `b_i`) is not enrolled. Steps 1-3 are fixed at
   the **roster commit** and use no randomness.
4. **Seed (from a public randomness beacon, revealed after the roster
   commit).** The roster commit names a future beacon round: a specific
   [drand](https://drand.love) round `R` (League of Entropy, 30 s cadence)
   whose scheduled time falls inside the named flip week, after the
   snapshot. `seed = SHA256(drand_randomness(R))`, where `drand_randomness(R)`
   is the 32-byte hex value that beacon publishes at round `R`. The value
   is unpredictable until the round emits, so it does not exist when the
   roster, the pairing, the eligibility list, and the excluded-store list
   are all chosen. (Substitute: the NIST Randomness Beacon pulse at a named
   future timestamp; the roster commit fixes exactly one source and one
   round/pulse.)
5. For each merchant compute
   `t_i = HMAC-SHA256(seed, lowercase merchant UUID string)`, hex digest.
6. Within each pair, the merchant with the lexicographically smaller hex
   digest is assigned **treatment** (store memory); the other **control**
   (throwaway path). Arms are recorded in the **assignment commit**.

Why this cannot be gamed or re-rolled:

- The mechanism and eligibility criteria are frozen in this commit, in git
  history, before any roster exists.
- Merchant UUIDs are engine-minted at install time, before the pilot
  existed; no party chooses them.
- The seed comes from a public beacon whose value is **unknowable when the
  roster is committed**. Grinding is therefore impossible: there is no key
  or snapshot choice that can be searched against a known split, because
  the split-determining entropy has not been generated yet. The beacon
  value is externally verifiable (drand signatures / NIST pulses are
  publicly archived), so the seed cannot be fabricated after the fact.
- The roster commit binds one roster to one future beacon round; the §2.3
  slippage rule voids the roster (never reuses the round) if the flip week
  slips, so a deferred flip cannot redraw the arms.
- `b_i` and `v_i` come from frozen queries over historical data; the roster
  commit publishes them, so the sort order is auditable.
- Given the committed roster and the public beacon value, assignment is a
  pure function. Re-running it is the audit.

The W4 allowlist and the Phase-3 (W7) allowlist must equal the treatment
arm exactly, minus any merchants removed by a registered rollback (§2.5).
The operator asserts this equality at each enablement and weekly during
the pilot; an uncorrected unregistered mismatch is a protocol breach (§9).

### 2.5 Mid-pilot churn, uninstall, and redaction

- **Intention-to-treat at the merchant level:** once enrolled, a merchant
  stays in its assigned arm for analysis regardless of what happens later.
  Treated runs that fall back to the throwaway path (errors, caps,
  rollback) remain treated. A **registered rollback** (allowlist removal
  under the plan's W4 rollback/abort mechanism, i.e. store-memory write
  errors over the abort threshold, recorded in a visible commit) is
  sanctioned divergence: the merchant stays in the treatment arm for
  analysis and does not trigger §9 item 3; no pair is dropped. No
  post-randomization exclusion may be based on any outcome-correlated
  quantity.
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
- `sent_at`: engine#192-C sets `sent_at` only on the direct-acceptance
  path; no component assigns it on E4c webhook reconciliation. For
  attempts reconciled to `accepted` by E4c, this registration freezes the
  attempt row's creation timestamp (the instant the row was created
  `pending`, i.e. send initiation; always present, written before any
  outcome is known) as `sent_at`. That value serves everywhere this
  document uses `sent_at`: the §3.2 window predicate, the 7-day
  attribution anchor, and Q1.

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

PRD §7 lists one further secondary, the insight `confidence`
distribution. Its exclusion here is deliberate and registered:
`insight.confidence` is `assess_confidence_heuristic(cart)`, computed
from the cart alone and therefore treatment-insensitive (the same
property that disqualified it as the W3 gate metric), so it cannot
inform this pilot.

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

The formula is evaluated per roster (§4.5): `v = v_bar` of the roster
under evaluation, the **harmonic mean** of that roster's merchants' weekly
accepted volumes (`v_bar = n / sum(1/v_i)` over the roster's `n`
merchants), rounded down to one decimal. The harmonic mean, not the
median, is the correct conservative aggregate: per-merchant sampling
variance enters the power condition through `1/m_i = 1/(v_i*w)`, so the
pooled `1/m` term is the average of `1/v_i`, whose reciprocal is exactly
the harmonic mean. Under heterogeneous volumes the median can materially
overstate power (a roster with a few low-volume merchants needs far more
duration than its median volume implies). `p0` is the pooled baseline
conversion over that same roster (total recovered / total denominator,
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
  AND COALESCE(sent_at, created_at) >= :snapshot - INTERVAL '56 days'
  AND COALESCE(sent_at, created_at) <  :snapshot
GROUP BY shopify_store_id;
```

Until `recovery_attempt` holds 56 days of history, the proxy is
provider-accepted recovery-email sends over the same window from the send
pipeline's persisted records (Inkwell send spine; a send counts iff the
SendGrid API call returned success). `v_i = accepted_56d / 8` (per week).

**`sent_at` COALESCE (normative, applies to Q1, Q2, and §3.2).**
`COALESCE(sent_at, created_at)` implements the §3.1 frozen rule: an
E4c-reconciled attempt has `sent_at` NULL (engine#192-C writes `sent_at`
only on direct acceptance), so the attempt's creation timestamp stands in.
Engine #192-C5 SHOULD persist the creation instant into `sent_at` on
reconciliation, which turns every COALESCE here into a no-op; until it
does, the COALESCE is what stops reconciled-accepted rows from silently
dropping out of these denominators and the §3.2 attribution window. The
engine-side derivation of the primary metric MUST use the same COALESCE.

**Q2 - per-merchant baseline recovery rate, trailing 56 days.** This must
estimate the SAME quantity the pilot measures (§3.2): recoveries per
provider-accepted send, attributed within 7 days of the send. It is
therefore computed over the accepted-send spine (denominator identical to
Q1's `accepted_56d`) and anchored at the send time, NOT over all
abandonment episodes anchored at `checkout_started_at`. An episode-level
rate would count episodes that never produced an accepted send and orders
that preceded any email, estimating a different and biased baseline. The
construct is applied to every merchant symmetrically and feeds only pairing
and power inputs, never the confirmatory contrast. Canonical once E4b is
live:

```sql
SELECT a.shopify_store_id,
       COUNT(*) AS accepted_56d,
       COUNT(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM orders o
         WHERE o.shopify_store_id     = a.shopify_store_id
           -- a.anonymous_id resolves via the attempt's episode (event_id)
           AND o.matched_anonymous_id = a.anonymous_id
           AND o.created_at >  COALESCE(a.sent_at, a.created_at)
           AND o.created_at <= COALESCE(a.sent_at, a.created_at) + INTERVAL '7 days'
       )) AS recovered_56d
FROM recovery_attempt a
WHERE a.status = 'accepted'
  AND COALESCE(a.sent_at, a.created_at) >= :snapshot - INTERVAL '56 days'
  AND COALESCE(a.sent_at, a.created_at) <  :snapshot
GROUP BY a.shopify_store_id;
```

Until `recovery_attempt` predates the snapshot by 56 days, the proxy is the
same provider-accepted send spine Q1 uses (Inkwell send records; a send
counts iff the SendGrid API call returned success), with recovery
attributed within 7 days of the send time and the shopper's `anonymous_id`
carried from the analysis; the roster commit records the exact join used.
`b_i = recovered_56d / accepted_56d`; pooled `p0 = sum(recovered_56d) /
sum(accepted_56d)`, computed separately per roster (`p0_A` over roster A,
`p0_B` over roster B).

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
tier, pairs `J`, and harmonic-mean weekly accepted volume per merchant
`v_bar` (§4.2).
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

Honest reading: at the 1.5x and 1.75x tiers the `J_min` row is never
feasible within the cap; merchant count, not duration, is the lever
there. Only the 2.0x tier is feasible at its floor rows (from J = 4 at
`v_bar` = 50, and at the displayed J = 5 from `v_bar` = 25). The PRD's
5+5 floor supports only the 2.0x tier at moderate-to-high volume.

### 4.5 Mechanical sizing ladder (zero post-hoc discretion)

Inputs: roster A and roster B (§2.2 volume floors), their sizes `K_A` and
`K_B`, and per-roster values from §4.3: pooled baselines `p0_A`, `p0_B`
and harmonic-mean weekly volumes `v_bar_A`, `v_bar_B` (§4.2), each computed
over that roster's merchants. Pairs: `J_A = floor(K_A / 2)`,
`J_B = floor(K_B / 2)`. Each step is evaluated with its own roster's
values: `p0_A`, `v_bar_A`, `J_A` for the A steps and `p0_B`, `v_bar_B`,
`J_B` for the B steps (roster B is a superset at a lower volume floor, so
`v_bar_B <= v_bar_A`; the two evaluations are not interchangeable).
Evaluate the steps in order; **the first satisfiable step launches with
its tier and duration**:

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
at enrollment (fixes roster, pairs, and J) and once at Phase-3 enablement.
The re-run's protocol is frozen: with the roster fixed at enrollment, only
the steps whose roster equals the enrolled roster are evaluable (keeping
that roster's week floor and its frozen J); `v_bar` is re-measured as the
enrolled roster's harmonic mean (§4.2) over the then-current trailing 56
days; `p0` is
**not** re-measured (the enrollment value for the enrolled roster is
reused); pairing and J never change. If no evaluable step passes, the L7
outcome applies: Phase 3 does not launch as confirmatory without an
amendment per the preceding paragraph. The re-run is blinded sample-size
re-estimation on pre-exposure data: no treatment-vs-control outcome
exists before Phase 3, because reads are off until W7 flips. Its output
is committed (the Phase-3 sizing commit, recording `T3`, the re-measured
`v_bar`, the chosen step, and `w`) before the W7 flag flips.

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
  Carlo with 100,000 draws from numpy `default_rng` (PCG64), seed 61,
  p-value `(1 + #{T* >= T_obs}) / (N + 1)`. Exhaustive p-value:
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
- **Tooling:** the analysis script implementing this section is merged to
  the repo before Phase-3 enablement (the same freeze point as §9, before
  any treatment exposure exists) and run once at the §3.2 analysis
  instant.

## 7. Operator fill-in (to be completed in the roster, assignment, and sizing commits)

Enrollment (roster commit, merges before the W4 allowlist is populated):

| Input | Value |
|---|---|
| Snapshot timestamp (§2.3) | (fill) |
| SQL actually run for Q1/Q2 | (fill) |
| K_A, K_B | (fill) |
| Pooled p0_A, p0_B | (fill) |
| v_bar_A, v_bar_B (harmonic mean, §4.2) | (fill) |
| Ladder step selected | (fill) |
| J (pairs, from the selected roster) | (fill) |
| Assertion: no stores beyond the §2.2 frozen exclusion list | (fill) |
| Intended flip ISO week (§2.3) | (fill) |
| Beacon source + named future round/pulse R (§2.4) | (fill) |
| SHA256 of the sorted roster UUID list | (fill) |

Per-merchant roster table (roster commit): merchant UUID, `accepted_56d`,
`recovered_56d`, `b_i`, `v_i`, pair index. Arms are absent here by design;
the seed does not exist yet.

Assignment (assignment commit, merges after the beacon reveals, before any
arm-labeled data is used):

| Input | Value |
|---|---|
| Revealed beacon value at round R | (fill) |
| `seed = SHA256(beacon value)` | (fill) |
| Per-merchant `HMAC-SHA256(seed, lowercase UUID)` digest + arm | (fill) |

Phase-3 sizing commit (merges before the W7 flag flips): `T3`, re-measured
`v_bar` for the enrolled roster, the evaluable ladder step selected, final
`w`, and the allowlist-equality assertion (§2.4).

## 8. Relationship to other gates

P0 is a paper gate produced by this merge. It joins G0, G3, E2a, E3a, and
W4E in gating W4's production enablement, and it is a named input to P1
(which executes the design frozen here). The metric definitions in §3
depend on E4b (attempt schema), E4c (reconciliation and delivery states),
and engine#192-D (outcome derivation) being live before Phase 3; those are
already sequenced ahead of the pilot by the implementation plan.

## 9. Registration integrity

The confirmatory status of the pilot is void if any of the following
happens without an amendment commit permitted by the staged windows at
the end of this section:

1. The assignment recomputation from the roster commit plus the public
   beacon value at the named round does not reproduce the arms recorded in
   the assignment commit.
2. The roster or pairing changes after the roster commit, or the arms
   change after the assignment commit, or the named beacon round changes.
3. The W4/W7 allowlist diverges from the treatment arm through
   **unregistered** divergence (a non-treatment merchant on the
   allowlist, or a removal not recorded as a registered rollback per
   §2.5) and is not corrected within one week. A registered rollback is
   sanctioned: the merchant stays in ITT and no pair is dropped.
4. `ATTRIBUTION_WINDOW_DAYS` is changed from 7 mid-pilot.
5. The analysis deviates from §6, the metrics from §3, or the window from
   the committed `T3` and `w`.
6. Any week of pilot data is excluded or reweighted.
7. The Q1/Q2 semantics are altered after the roster commit (the SQL text
   may only be adapted to schema naming, with the semantics of §4.3
   preserved and the change recorded).
8. The W4 allowlist is first populated outside the ISO week named in the
   roster commit and the pilot proceeds on that roster instead of a fresh
   roster commit per the §2.3 slippage rule.

**Amendment windows (staged):** per-merchant, arm-labeled attempt and
conversion data accumulate from Phase 2 onward, so a single window open
until Phase-3 enablement would permit outcome-informed rule changes. §2
and §3 (assignment, eligibility, exclusion rules, metric definitions)
therefore close at the roster commit: after it merges they may not be
amended, only voided and redrawn per §2.3. Between the roster commit and
Phase-3 enablement, the only permitted amendments are those §4.5 itself
provides for (the Phase-3 sizing re-run, blinded and pre-exposure, and at
L7 a non-confirmatory-launch amendment) and operational clarifications
that alter no definition in §2, §3, or §6. Every amendment is a visible
commit to this file stating what changed, why, and what pilot data
existed when it was made. After Phase-3 enablement, nothing in this
document changes.

The **assignment commit** (§2.3, §7) is the mechanical second step of the
randomization, not an amendment: it records the revealed beacon value and
the arms that value plus the already-frozen roster determine, and adds no
discretion. It merges after the beacon reveals and before any arm-labeled
data is used.
