"""Fixed evaluation policy: synthetic evidence cannot satisfy promotion."""

from datetime import datetime, timezone

from .data import TARGET, digest, load, split, vector
from .model import bootstrap_difference, calibrate, fit, metrics, predict

POLICY = {
    "version": "conversion_promotion_v1",
    "min_train": 1000,
    "min_calibration": 300,
    "min_test": 300,
    "min_class": 30,
    "min_merchants_per_split": 3,
    "max_ece": 0.08,
    "max_merchant_ece": 0.15,
    "min_merchant_test": 50,
    "min_subgroup_test": 50,
    "max_age_days": 90,
    "required_improvement": "positive_lower_95ci_merchant_cluster_bootstrap_brier",
}


def train(paths, train_through, calibration_through):
    rows, datasets, policies = load(paths)
    datasets = [
        {k: v for k, v in d.items() if k != "assignment_units"} for d in datasets
    ]
    partitions = split(rows, train_through, calibration_through)
    report = {
        "schema_version": "conversion_training_v1",
        "target": TARGET,
        "feature_version": "recovery_features_v1",
        "horizon_seconds": 604800,
        "promotion_policy": POLICY,
        "datasets": datasets,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "train_through": train_through,
        "calibration_through": calibration_through,
        "origin": "real"
        if datasets and all(d["origin"] == "real" for d in datasets)
        else "synthetic",
        "total_rows": len(rows),
        "excluded_rows": len(rows) - sum(map(len, partitions.values())),
        "split_counts": {k: len(v) for k, v in partitions.items()},
        "split_merchants": {
            k: sorted({r["merchant_id"] for r in v}) for k, v in partitions.items()
        },
        "status": "insufficient_data",
        "promotion_eligible": False,
        "gates": {},
        "models": {},
        "evaluations": {},
    }
    # Tiny fixtures can exercise mechanics; promotion always uses the fixed
    # production gates below. No command-line option can lower them.
    if any(
        len(v) < 20 or {r["without_vakaru_label"] for r in v} != {0, 1}
        for v in partitions.values()
    ):
        report["model_id"] = digest(report)
        return report
    for name, semantic in (("event_only", False), ("event_and_semantic", True)):
        x = {
            k: [vector(r["features"], semantic) for r in v]
            for k, v in partitions.items()
        }
        y = {k: [r["without_vakaru_label"] for r in v] for k, v in partitions.items()}
        model = fit(x["train"], y["train"])
        calibrate(model, x["calibration"], y["calibration"])
        test_predictions = predict(model, x["test"])
        evaluation = metrics(y["test"], test_predictions)
        evaluation["by_merchant"] = {}
        for merchant in report["split_merchants"]["test"]:
            pairs = [
                (label, p)
                for row, label, p in zip(
                    partitions["test"], y["test"], test_predictions
                )
                if row["merchant_id"] == merchant
            ]
            evaluation["by_merchant"][merchant] = metrics(
                [a for a, _ in pairs], [b for _, b in pairs]
            )
        evaluation["by_semantic_availability"] = {}
        for available in (False, True):
            pairs = [
                (label, p)
                for row, label, p in zip(
                    partitions["test"], y["test"], test_predictions
                )
                if row["features"]["semantic_available"] == available
            ]
            evaluation["by_semantic_availability"][str(available).lower()] = metrics(
                [a for a, _ in pairs], [b for _, b in pairs]
            )
        report["models"][name] = model
        report["evaluations"][name] = evaluation
    base, candidate = (
        report["models"]["event_only"],
        report["models"]["event_and_semantic"],
    )
    test = partitions["test"]
    interval = bootstrap_difference(
        [r["without_vakaru_label"] for r in test],
        predict(base, [vector(r["features"], False) for r in test]),
        predict(candidate, [vector(r["features"]) for r in test]),
        [r["merchant_id"] for r in test],
    )
    report["brier_improvement_95ci"] = interval
    evaluated = report["evaluations"]["event_and_semantic"]
    gates = {
        "real_outcomes": report["origin"] == "real",
        "sample_sizes": all(
            len(v) >= POLICY["min_" + k]
            and min(
                sum(r["without_vakaru_label"] for r in v),
                len(v) - sum(r["without_vakaru_label"] for r in v),
            )
            >= POLICY["min_class"]
            for k, v in partitions.items()
        ),
        "merchant_coverage": all(
            len(v) >= POLICY["min_merchants_per_split"]
            for v in report["split_merchants"].values()
        ),
        "brier_improvement": interval is not None and interval[0] > 0,
        "calibration": evaluated["ece"] <= POLICY["max_ece"],
        "merchant_stability": all(
            m["n"] >= POLICY["min_merchant_test"]
            and m["ece"] <= POLICY["max_merchant_ece"]
            for m in evaluated["by_merchant"].values()
        ),
        "subgroup_coverage": all(
            m["n"] >= POLICY["min_subgroup_test"]
            and m["ece"] <= POLICY["max_merchant_ece"]
            for m in evaluated["by_semantic_availability"].values()
        ),
    }
    report.update(
        status="evaluated", gates=gates, promotion_eligible=all(gates.values())
    )
    report["model_id"] = digest(report)
    return report
