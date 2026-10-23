import pytest
from cart_recovery.adaptive.evaluation import report


def row(i=0):
    return {
        "journey_id": str(i),
        "merchant_id": "synthetic-merchant",
        "split": "test",
        "label_source": "synthetic",
        "representative": True,
        "hazardous": False,
        "acceptable_actions": ["message:shipping"],
        "rules": {"action": "message", "purpose_id": "shipping"},
        "typesafe": {"action": "message", "purpose_id": "shipping"},
        "repetitive_rules": False,
        "repetitive_typesafe": False,
        "latency_ms": 10,
        "charged_micros": 1000,
        "usage_known": True,
        "cache_hit": False,
        "binding": "fixture-only",
        "frozen_before_test": False,
    }


def test_small_synthetic_replay_cannot_qualify():
    r = report([row()])
    assert not r["selection_gates_pass"] and not r["production_qualified"]
    assert "independent_human_labels_missing" in r["blocked_gates"]
    assert "independent_hazard_test_below_300" in r["blocked_gates"]


def test_duplicates_and_merchant_leakage_rejected():
    with pytest.raises(ValueError):
        report([row(), row()])
    b = row(1)
    b["split"] = "development"
    assert "merchant_split_leakage" in report([row(), b])["blocked_gates"]


def test_always_abstain_cannot_look_useful():
    b = row()
    b["typesafe"] = {"action": "review"}
    r = report([b])
    assert (
        r["automatic_coverage"] == 0
        and r["message_coverage"] == 0
        and r["useful_action_delta"] == -1
    )


def test_zero_failures_still_requires_independent_safety_evidence():
    rows = []
    for i in range(300):
        b = row(i)
        b.update(
            hazardous=True,
            representative=False,
            acceptable_actions=["wait"],
            typesafe={"action": "wait"},
        )
        rows.append(b)
    r = report(rows)
    assert 0 < r["zero_failure_one_sided_95_upper"] < 0.01
    assert not r["selection_gates_pass"] and not r["production_qualified"]
