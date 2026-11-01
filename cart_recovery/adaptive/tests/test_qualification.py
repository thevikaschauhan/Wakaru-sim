import pytest
from cart_recovery.adaptive.qualification import report


def row():
    return dict(
        case_id="fixture",
        merchant_id="fixture",
        task="TF05",
        variant="full",
        split="test",
        gold=False,
        prediction="pass",
        confidence=0.99,
        adjudication_ref="",
        label_source="synthetic",
        binding="fixture",
        frozen=True,
        critical=True,
        claim_coverage=False,
    )


def test_synthetic_confident_wrong_judgment_never_qualifies():
    r = report([row()])
    assert not r["production_qualified"]
    m = r["metrics"]["TF05:full"]
    assert m["critical_false_passes"] == 1 and m["ece"] == 0.99
    assert (
        "claim_coverage_incomplete" in r["blocked_gates"]
        and "independent_adjudication_missing" in r["blocked_gates"]
    )


def test_duplicates_and_invalid_probability_rejected():
    with pytest.raises(ValueError):
        report([row(), row()])
    r = row()
    r["confidence"] = float("nan")
    with pytest.raises(ValueError):
        report([r])


def test_abstention_is_coverage_loss_not_pass():
    r = row()
    r["prediction"] = "abstain"
    m = report([r])["metrics"]["TF05:full"]
    assert m["coverage"] == 0 and m["accuracy"] is None


def test_ablations_must_share_adjudicated_identity():
    a = row()
    b = row()
    b["variant"] = "rules"
    b["gold"] = True
    with pytest.raises(ValueError):
        report([a, b])
