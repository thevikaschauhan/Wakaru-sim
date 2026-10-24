"""Task calibration and paired ablations from a frozen, independently labeled set.
No network, provider, threshold mutation or runtime enablement. Synthetic labels
exercise mechanics only and never qualify a release.
"""

from collections import defaultdict
from statistics import mean
import math

TASKS = {f"TF{i:02}" for i in range(1, 8)}
VARIANTS = {"full", "rules", "without_semantic_selection", "without_draft_checks"}


def report(rows):
    groups = defaultdict(list)
    seen = set()
    splits = defaultdict(set)
    issues = set()
    identities = {}
    for r in rows:
        required = {
            "case_id",
            "merchant_id",
            "task",
            "variant",
            "split",
            "gold",
            "prediction",
            "confidence",
            "adjudication_ref",
            "label_source",
            "binding",
            "frozen",
            "critical",
            "claim_coverage",
        }
        if (
            set(r) != required
            or r["task"] not in TASKS
            or r["variant"] not in VARIANTS
            or r["split"] not in {"calibration", "test"}
        ):
            raise ValueError("invalid row")
        if (
            not isinstance(r["gold"], bool)
            or r["prediction"] not in {"pass", "fail", "abstain"}
            or isinstance(r["confidence"], bool)
            or not isinstance(r["confidence"], (int, float))
            or not math.isfinite(r["confidence"])
            or not 0 <= r["confidence"] <= 1
        ):
            raise ValueError("invalid judgment")
        if (
            any(
                not isinstance(r[k], bool)
                for k in ("critical", "claim_coverage", "frozen")
            )
            or any(
                not isinstance(r[k], str) or not r[k]
                for k in ("case_id", "merchant_id", "binding")
            )
            or not isinstance(r["adjudication_ref"], str)
            or r["label_source"] not in {"human_adjudicated", "synthetic"}
        ):
            raise ValueError("invalid evidence metadata")
        identity = (r["case_id"], r["task"])
        label = (r["merchant_id"], r["gold"], r["split"], r["critical"])
        if identity in identities and identities[identity] != label:
            raise ValueError("ablation identity or label mismatch")
        identities[identity] = label
        key = (r["case_id"], r["task"], r["variant"])
        if key in seen:
            raise ValueError("duplicate case variant")
        seen.add(key)
        splits[r["merchant_id"]].add(r["split"])
        if r["label_source"] != "human_adjudicated" or not r["adjudication_ref"]:
            issues.add("independent_adjudication_missing")
        if r["frozen"] is not True:
            issues.add("binding_not_frozen")
        if r["claim_coverage"] is not True:
            issues.add("claim_coverage_incomplete")
        groups[(r["task"], r["variant"], r["split"])].append(r)
    if any(len(v) > 1 for v in splits.values()):
        issues.add("merchant_split_leakage")
    results = {}
    for task in sorted(TASKS):
        sets = []
        for variant in sorted(VARIANTS):
            test = groups[(task, variant, "test")]
            sets.append({r["case_id"] for r in test})
            if not test:
                issues.add("missing_task_or_ablation")
            if not groups[(task, variant, "calibration")]:
                issues.add("missing_calibration_split")
            decided = [r for r in test if r["prediction"] != "abstain"]
            critical = [r for r in test if r["critical"] and not r["gold"]]
            false_pass = sum(r["prediction"] == "pass" for r in critical)
            bins = []
            for i in range(10):
                cohort = [r for r in decided if min(int(r["confidence"] * 10), 9) == i]
                if cohort:
                    bins.append(
                        (
                            len(cohort),
                            mean(r["confidence"] for r in cohort),
                            mean(
                                (r["prediction"] == "pass") == r["gold"] for r in cohort
                            ),
                        )
                    )
            results[task + ":" + variant] = {
                "test_n": len(test),
                "coverage": len(decided) / len(test) if test else None,
                "accuracy": mean(
                    (r["prediction"] == "pass") == r["gold"] for r in decided
                )
                if decided
                else None,
                "ece": sum(n * abs(c - a) for n, c, a in bins) / len(decided)
                if decided
                else None,
                "critical_n": len(critical),
                "critical_false_passes": false_pass,
                "zero_failure_95_upper": 1 - 0.05 ** (1 / len(critical))
                if critical and not false_pass
                else None,
            }
            if len(critical) < 300 or false_pass:
                issues.add("critical_safety_unqualified")
            if len({r["binding"] for r in test}) != 1:
                issues.add("mixed_or_missing_test_binding")
        if any(s != sets[0] for s in sets[1:]):
            issues.add("unpaired_ablation_cases")
    # Threshold approval is external to descriptive metrics. Never fit on the test split.
    issues.add("independent_threshold_and_slice_review_required")
    return {
        "schema_version": "af11_task_qualification_v1",
        "metrics": results,
        "blocked_gates": sorted(issues),
        "production_qualified": False,
        "enablement_authorized": False,
    }
