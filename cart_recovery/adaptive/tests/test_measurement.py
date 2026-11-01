import copy
from uuid import uuid4
from .test_planner import setup, evaluate
from .fixtures import fixture
from ..measurement import measurement
from ..journal import Conflict
import pytest


def test_measurement_counts_retry_freezes_and_shadow(setup, monkeypatch):
    p, calls = setup()
    b = fixture()
    evaluate(p, b, "measurement")
    merchant, journey = b["context"]["merchant_id"], b["context"]["journey_id"]
    m = measurement(p.journal, merchant, journey)
    assert len(m["items"]) == 1
    assert m["items"][0]["known_micros"] > 0
    assert m["items"][0]["unknown_attempts"] == 0
    evaluate(p, b, "retry")
    assert len(measurement(p.journal, merchant, journey)["items"]) == 1
    assert len(calls) == 1
    assert measurement(p.journal, str(uuid4()), journey)["items"] == []
    shadow, shadow_calls = setup(env="shadow-"+str(uuid4()))
    evaluate(shadow, b, "shadow")
    monkeypatch.setenv("AF10_SHADOW_ENVIRONMENT", shadow.journal.environment)
    combined = measurement(p.journal, merchant, journey)
    assert sorted(x["traffic"] for x in combined["items"]) == ["serving", "shadow"]
    assert len(shadow_calls) == 1
    monkeypatch.setenv("AF10_SHADOW_ENVIRONMENT", "production")
    with pytest.raises(Conflict):
        measurement(p.journal, merchant, journey)


def test_unknown_cost_retains_charge(setup):
    import httpx
    p, _ = setup(handler=lambda _: httpx.Response(200, json={"invalid": True}))
    b = fixture()
    evaluate(p, b, "unknown-cost")
    item = measurement(p.journal, b["context"]["merchant_id"], b["context"]["journey_id"])["items"][0]
    assert item["known_micros"] == 0
    assert item["reserved_micros"] > 0
    assert item["unknown_attempts"] == 1
