"""Deterministic pipeline fixtures. These are never commercial model evidence."""

from datetime import datetime, timedelta, timezone
import hashlib
import random
from pathlib import Path
import uuid

from .data import OBSERVED, SEMANTIC, TARGET, canonical, digest, seal


def cohort(directory, per_merchant=240):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rng, selected, counts = random.Random(1701), [], {k: 0 for k in range(5)}
    candidate = 0
    while min(counts.values()) < 3:
        merchant = str(uuid.UUID(int=1000 + candidate))
        candidate += 1
        group = int(hashlib.sha256(merchant.encode()).hexdigest(), 16) % 5
        if counts[group] == 3:
            continue
        counts[group] += 1
        selected.append((merchant, group))
    paths = []
    for merchant, group in selected:
        start = datetime(
            2026, 1 if group < 3 else 2 if group == 3 else 3, 1, tzinfo=timezone.utc
        )
        policy = {
            "version": "recovery_experiment_v1",
            "experiment_id": "synthetic-only",
            "starts_at": (start - timedelta(hours=1)).isoformat(),
            "ends_at": (start + timedelta(days=1)).isoformat(),
            "analysis_at": (start + timedelta(days=10)).isoformat(),
            "registered_at": (start - timedelta(days=1)).isoformat(),
            "baseline_rate": 0.2,
            "minimum_effect": 0.1,
            "min_per_arm": 300,
            "mode": "execution",
            "policy_sha256": "a" * 64,
        }
        rows = []
        for i in range(per_merchant):
            at = start + timedelta(seconds=i)
            unit = hashlib.sha256(f"{merchant}:{i}".encode()).hexdigest()
            available = i % 3 != 0
            signal = rng.choice((0.1, 0.3, 0.7, 0.9))
            features = {
                "version": "recovery_features_v1",
                "observed": {
                    k: (
                        rng.randrange(1, 10) if k == "event_count" else rng.randrange(2)
                    )
                    for k in OBSERVED
                },
                "semantic": {
                    k: (signal if k == "price_sensitivity" else 0.5) if available else 0
                    for k in SEMANTIC
                },
                "semantic_available": available,
            }
            # A deliberately learnable synthetic relationship; not shopper truth.
            probability = 0.5 if not available else 0.05 + 0.9 * signal
            arm = "no_contact" if i % 2 == 0 else "recovery"
            paid = int(
                rng.random()
                < min(0.99, probability + (0.05 if arm == "recovery" else 0))
            )
            row = {
                "decision_id": str(uuid.uuid5(uuid.UUID(merchant), str(i))),
                "unit_sha256": unit,
                "decided_at": at.isoformat(),
                "mature_at": (at + timedelta(days=8)).isoformat(),
                "snapshot_sha256": "b" * 64,
                "input_sha256": "c" * 64,
                "policy_version": "synthetic-policy-v1",
                "assigned_action": "no_action",
                "features": features,
                "features_sha256": digest(features),
                "assignment": {
                    "version": "recovery_assignment_v1",
                    "experiment_id": "synthetic-only",
                    "unit_sha256": unit,
                    "arm": arm,
                    "probability": 0.5,
                    "assigned_at": at.isoformat(),
                    "protected_through": policy["analysis_at"],
                    "policy_sha256": policy["policy_sha256"],
                    "mode": "execution",
                },
                "first_unit_decision": True,
                "exposure": {
                    "planned_contact": arm == "recovery",
                    "channel": "email",
                    "offer_id": None,
                    "possible_contact": arm == "recovery",
                    "status": "no_vakaru_contact_recorded",
                    "contaminated": False,
                    "provider_accepted": arm == "recovery",
                    "delivered": False,
                    "click_recorded": False,
                    "human_exposure": "unknown",
                    "background_marketing": "unknown",
                    "cost_status": "unknown",
                    "event_ids": [],
                },
                "feedback": [],
                "outcome_status": "positive" if paid else "negative",
                "outcome_reason": "synthetic_pipeline_fixture",
                "paid_label": paid,
                "without_vakaru_label": paid if arm == "no_contact" else None,
                "target": TARGET,
                "outcome_revision_ids": [i + 1] if paid else [],
                "coverage_run_id": None if paid else 1,
                "accounting": {
                    "received_less_refunds": {},
                    "net_value": None,
                    "status": "costs_unknown",
                },
            }
            rows.append(seal(row, "row_sha256"))
        dataset = {
            "schema_version": "recovery_learning_v2",
            "merchant_id": merchant,
            "store_id": 1,
            "as_of": "2026-04-01T00:00:00Z",
            "decisions_from": start.isoformat(),
            "decisions_through": (start + timedelta(days=1)).isoformat(),
            "engine_source_sha256": "d" * 64,
            "label_version": "identified_customer_paid_order_7d_v1",
            "horizon_seconds": 604800,
            "grace_seconds": 86400,
            "origin": "synthetic",
            "rows": rows,
            "assignment_units": [r["assignment"] for r in rows],
            "experiment_policies": [policy],
        }
        seal(dataset, "dataset_sha256")
        path = directory / f"{merchant}.json"
        with path.open("xb") as output:
            output.write(canonical(dataset) + b"\n")
        path.chmod(0o600)
        paths.append(path)
    return paths


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()
    print("\n".join(map(str, cohort(args.directory))))
