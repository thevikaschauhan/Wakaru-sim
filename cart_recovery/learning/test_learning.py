from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from .data import digest, load, seal, split
from .experiment import analyze
from .registry import Registry
from .synthetic import cohort
from .training import train


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("learning")
    paths = cohort(root / "cohort")
    artifact = train(paths, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
    return root, paths, artifact


def test_full_pipeline_and_synthetic_promotion_fence(trained):
    root, paths, artifact = trained
    assert artifact["status"] == "evaluated"
    assert artifact["split_counts"] == {"train": 1080, "calibration": 360, "test": 360}
    assert (
        artifact["evaluations"]["event_and_semantic"]["brier"]
        < artifact["evaluations"]["event_only"]["brier"]
    )
    assert artifact["brier_improvement_95ci"][0] > 0
    assert not artifact["promotion_eligible"] and not artifact["gates"]["real_outcomes"]
    assert all(
        v["n"] == 120
        for v in artifact["evaluations"]["event_and_semantic"]["by_merchant"].values()
    )
    registry = Registry(root / "models.db")
    key = registry.register(artifact)
    assert registry.register(artifact) == key
    row = load(paths)[0][0]
    estimate = registry.estimate(
        key, row["features"], datetime.now(timezone.utc).isoformat()
    )
    assert estimate["status"] == "not_available" and estimate["probability"] is None
    with pytest.raises(ValueError, match="promotion_gates_failed"):
        registry.promote(key, "synthetic-reviewer", "test-only")
    with sqlite3.connect(registry.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE models SET artifact='{}' WHERE id=?", (key,))
    assert registry.delete_merchant(row["merchant_id"]) == 1
    assert registry.artifact(key) is None
    # A later re-import cannot resurrect a revoked training lineage.
    with pytest.raises(ValueError, match="training_tenant_erased"):
        registry.register(artifact)
    assert (
        registry.estimate(key, row["features"], datetime.now(timezone.utc).isoformat())[
            "probability"
        ]
        is None
    )


def test_split_purges_horizon_and_rejects_duplicate_units(trained):
    _, paths, _ = trained
    rows, _, _ = load(paths)
    parts = split(rows, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
    merchants = [{r["merchant_id"] for r in parts[key]} for key in parts]
    assert (
        not merchants[0] & merchants[1]
        and not merchants[1] & merchants[2]
        and not merchants[0] & merchants[2]
    )
    with pytest.raises(ValueError, match="duplicate_assignment_unit"):
        split(rows + [rows[0]], "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
    altered = deepcopy(rows)
    for r in altered:
        r["mature_at"] = "2026-04-01T00:00:00Z"
    assert not split(altered, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")["train"]
    # A label may be mature but only observed after the fitting cutoff.
    late = deepcopy(rows)
    for row in late:
        row["dataset_as_of"] = "2026-04-01T00:00:00Z"
    blocked = split(late, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
    assert not blocked["train"] and not blocked["calibration"]
    assert len(blocked["test"]) == 360


def test_contamination_hash_and_unmatured_gates(trained, tmp_path):
    _, paths, _ = trained
    original = json.loads(paths[0].read_text())
    for change in (
        lambda r: r["exposure"].update(contaminated=True),
        lambda r: r["assignment"].update(arm="recovery"),
        lambda r: r.update(mature_at="2027-01-01T00:00:00Z"),
    ):
        value = deepcopy(original)
        change(value["rows"][0])
        seal(value["rows"][0], "row_sha256")
        seal(value, "dataset_sha256")
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load([path])
    original["rows"][0]["features"]["semantic"]["price_sensitivity"] = 0.999
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="invalid_dataset_sha256"):
        load([path])


def test_four_serving_states_and_feature_bounds(trained, tmp_path):
    _, paths, artifact = trained
    rows, _, _ = load(paths)
    registry = Registry(tmp_path / "states.db")
    row = rows[1]
    now = datetime.now(timezone.utc).isoformat()
    assert (
        registry.estimate("absent", row["features"], now)["status"] == "not_available"
    )
    small = deepcopy(artifact)
    small.update(status="insufficient_data", promotion_eligible=False)
    small.pop("model_id")
    small["model_id"] = digest(small)
    registry.register(small)
    assert (
        registry.estimate(small["model_id"], row["features"], now)["status"]
        == "insufficient_data"
    )
    # Isolated registry fixture bypasses promotion solely to exercise serving.
    # The public promotion method still rejects the synthetic artifact above.
    registry.register(artifact)
    with sqlite3.connect(registry.path) as db:
        db.execute(
            "INSERT INTO promotions VALUES(?,?,?,?)",
            (artifact["model_id"], "test-fixture", "test-only", now),
        )
    good = registry.estimate(artifact["model_id"], row["features"], now)
    assert good["status"] == "available" and 0 < good["probability"] < 1
    bad = deepcopy(row["features"])
    bad["observed"]["event_count"] = 501
    result = registry.estimate(artifact["model_id"], bad, now)
    assert result["status"] == "out_of_distribution" and result["probability"] is None
    assert (
        registry.estimate(
            artifact["model_id"], row["features"], "2026-01-01T00:00:00Z"
        )["status"]
        == "out_of_distribution"
    )


def test_itt_does_not_claim_synthetic_lift(trained):
    _, paths, _ = trained
    reports = analyze(paths)["experiments"]
    assert len(reports) == 15
    assert all(
        r["status"] == "insufficient_evidence"
        and r["itt_paid_rate_difference"] is None
        and r["incremental_net_value"] is None
        for r in reports
    )


def test_exact_net_value_and_currency_separation():
    from .experiment import net_value_difference

    def row(amount, currency="USD", status="complete"):
        return {
            "accounting": {
                "status": status,
                "net_value": amount,
                "received_less_refunds": {currency: amount},
            }
        }

    assert net_value_difference([row("0.1")], [row("0.3")]) == {
        "currency": "USD",
        "mean_difference": "0.2",
    }
    assert net_value_difference([row("0.1")], [row("0.3", "EUR")]) is None
    assert (
        net_value_difference([row("0.1")], [row("0.3", status="costs_unknown")]) is None
    )


def test_contaminated_controls_remain_in_itt(trained, tmp_path):
    _, paths, _ = trained
    data = json.loads(paths[0].read_text())
    first = data["rows"][0]
    first["without_vakaru_label"] = None
    first["exposure"].update(
        contaminated=True, possible_contact=True, provider_accepted=True
    )
    seal(first, "row_sha256")
    seal(data, "dataset_sha256")
    path = tmp_path / "contaminated.json"
    path.write_text(json.dumps(data))
    report = analyze([path])["experiments"][0]
    assert report["contaminated_holdout_units"] == 1
    assert report["arms"]["no_contact"]["assigned"] == 120
    assert report["arms"]["no_contact"]["mature_known"] == 120
    assert report["itt_paid_rate_difference"] is None


def test_missing_decision_is_censored_itt_unit(trained, tmp_path):
    _, paths, _ = trained
    data = json.loads(paths[0].read_text())
    data["rows"].pop(0)
    seal(data, "dataset_sha256")
    path = tmp_path / "missing-handoff.json"
    path.write_text(json.dumps(data))
    result = analyze([path])["experiments"][0]
    assert result["arms"]["no_contact"]["assigned"] == 120
    assert result["arms"]["no_contact"]["unknown"] == 1
    assert not result["gates"]["complete_labels"]
    assert result["itt_paid_rate_difference"] is None
