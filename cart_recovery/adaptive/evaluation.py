"""Offline, human-labeled selection gates. Never invokes a provider or enables policy.

Run: python -m cart_recovery.adaptive.evaluation rows.jsonl
Each row is an independent journey. Development/calibration/test merchants must
be disjoint. Report selection evidence separately from AF07 draft safety and
AF10 conversion experiments. Synthetic runs always remain unqualified.
"""

import json
import math
import random
import sys
from collections import defaultdict
from statistics import mean


def useful(decision, row):
    key = decision["action"] + (
        ":" + decision["purpose_id"] if decision["action"] == "message" else ""
    )
    return int(key in row["acceptable_actions"])


def report(rows):
    ids = set()
    merchant_splits = defaultdict(set)
    issues = []
    required = {
        "journey_id",
        "merchant_id",
        "split",
        "label_source",
        "representative",
        "hazardous",
        "acceptable_actions",
        "rules",
        "typesafe",
        "repetitive_rules",
        "repetitive_typesafe",
        "latency_ms",
        "charged_micros",
        "usage_known",
        "cache_hit",
        "binding",
        "frozen_before_test",
    }
    for row in rows:
        if set(row) != required:
            raise ValueError("invalid evaluation row")
        if row["journey_id"] in ids:
            raise ValueError("journey variants are not independent units")
        ids.add(row["journey_id"])
        merchant_splits[row["merchant_id"]].add(row["split"])
        if row["split"] not in {"development", "calibration", "test"}:
            raise ValueError("invalid split")
        if row["label_source"] != "human_adjudicated":
            issues.append("independent_human_labels_missing")
        if not row["frozen_before_test"]:
            issues.append("registry_not_frozen_before_test")
    if any(len(s) > 1 for s in merchant_splits.values()):
        issues.append("merchant_split_leakage")
    tests = [r for r in rows if r["split"] == "test"]
    rep = [r for r in tests if r["representative"]]
    haz = [r for r in tests if r["hazardous"]]
    if len(rows) < 600:
        issues.append("independent_corpus_below_600")
    if len(haz) < 300:
        issues.append("independent_hazard_test_below_300")
    if len({r["binding"] for r in tests}) != 1:
        issues.append("unfrozen_test_binding")
    diffs = [useful(r["typesafe"], r) - useful(r["rules"], r) for r in rep]
    ci = None
    delta = mean(diffs) if diffs else None
    if diffs:
        rng = random.Random(601)
        draws = sorted(mean(rng.choices(diffs, k=len(diffs))) for _ in range(2000))
        ci = [draws[49], draws[1949]]
    automatic = sum(r["typesafe"]["action"] != "review" for r in rep)
    messages = [r for r in rep if r["typesafe"]["action"] == "message"]
    baseline_messages = [r for r in rep if r["rules"]["action"] == "message"]
    severe = sum(
        r["typesafe"]["action"] == "message" and not useful(r["typesafe"], r)
        for r in haz
    )
    # Exact one-sided binomial upper bound for the required zero-failure case.
    hazard_upper = 1 - math.pow(0.05, 1 / len(haz)) if haz and severe == 0 else None
    repeat = mean(r["repetitive_typesafe"] for r in messages) if messages else None
    baseline_repeat = (
        mean(r["repetitive_rules"] for r in baseline_messages)
        if baseline_messages
        else None
    )
    cohorts = {}
    for cache in [False, True]:
        cohort = [r for r in tests if r["cache_hit"] == cache]
        latencies = sorted(r["latency_ms"] for r in cohort)
        cohorts["warm" if cache else "cold"] = {
            "n": len(cohort),
            "p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1]
            if latencies
            else None,
            "charged_micros": sum(r["charged_micros"] for r in cohort),
            "unknown_usage": sum(not r["usage_known"] for r in cohort),
        }
    if delta is None or delta < 0.05 or ci[0] <= 0:
        issues.append("usefulness_improvement_gate_not_met")
    if not rep or automatic / len(rep) < 0.3:
        issues.append("automatic_coverage_below_30_percent")
    if (
        repeat is None
        or baseline_repeat is None
        or repeat > 0.05
        or repeat > baseline_repeat
    ):
        issues.append("repetition_gate_not_met")
    if severe or hazard_upper is None or hazard_upper > 0.01:
        issues.append("hazard_false_pass_gate_not_met")
    if not cohorts["cold"]["n"] or any(
        c["p95_ms"] is not None and c["p95_ms"] > 5000 for c in cohorts.values()
    ):
        issues.append("planning_latency_gate_not_met")
    if any(not r["usage_known"] for r in tests):
        issues.append("usage_accounting_incomplete")
    return {
        "schema_version": "af06_offline_selection_report_v1",
        "independent_journeys": len(rows),
        "test_n": len(tests),
        "representative_test_n": len(rep),
        "hazard_test_n": len(haz),
        "useful_action_delta": delta,
        "paired_journey_bootstrap_95": ci,
        "automatic_coverage": automatic / len(rep) if rep else None,
        "message_coverage": len(messages) / len(rep) if rep else None,
        "review_count": len(rep) - automatic,
        "repetitive_message_rate": repeat,
        "baseline_repetitive_message_rate": baseline_repeat,
        "severe_false_passes": severe,
        "zero_failure_one_sided_95_upper": hazard_upper,
        "cohorts": cohorts,
        "blocked_gates": sorted(set(issues)),
        "selection_gates_pass": not issues,
        "production_qualified": False,
        "remaining_gates": [
            "AF07 full-text claim recall and draft safety",
            "independently reviewed task calibration and critical slices",
            "registered workload and budget reconciliation",
            "AF10 randomized paid-order and net-value contrast",
        ],
    }


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    print(json.dumps(report(rows), indent=2))
