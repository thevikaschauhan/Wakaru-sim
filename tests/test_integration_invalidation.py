import json
import time
from pathlib import Path
import pytest
from cart_recovery.integration_invalidation import VERSION, PATH, create_invalidating_service_app, validate_event
from cart_recovery.integration_privacy import PrivacyStore, PrivacyStop
from cart_recovery.intelligence.bounded_context import BoundedService, select_assistance
from test_bounded_context import fixture
from test_integration_service import sign


def test_c12_wire_validation():
    root = Path(__file__).resolve().parents[1]
    event = json.loads((root / "contracts/integrations/c12/invalidation.json").read_text())["event"]
    validate_event(event, event["merchant_id"], event["store_id"])
    for key, value in [("epoch", True), ("epoch", 0), ("cutoff_us", 9007199254740992), ("version", "next"), ("store_id", "18")]:
        with pytest.raises(ValueError):
            validate_event(dict(event, **{key: value}), event["merchant_id"], event["store_id"])


def test_c12_fence_http_restart_late_result_and_recapture(tmp_path):
    value, bound = fixture()
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    grant = dict(key_id="fixture", secret="c12-fixture-" + "x" * 32, merchant_id=bound["merchant"], store_id=bound["store"], audience="wakaru", operations=["POST /internal/integrations/analyze", "POST " + PATH])
    client = create_invalidating_service_app(enabled=True, grants=[grant], store=store).test_client()
    def call(event, auth=True):
        raw = json.dumps(event).encode()
        return client.post(PATH, data=raw, headers={"Authorization": sign(grant, raw, PATH)} if auth else {})
    raw = json.dumps(value).encode()
    assert client.post("/internal/integrations/analyze", data=raw, headers={"Authorization": sign(grant, raw)}).status_code == 200
    assert create_invalidating_service_app().test_client().post(PATH).status_code == 404
    cutoff = int(time.time() * 1000000)
    event = dict(version=VERSION, merchant_id=bound["merchant"], store_id=bound["store"], epoch=1, cutoff_us=cutoff)
    assert call(event, False).status_code == 401
    assert call(dict(event, store_id="18")).status_code == 409
    assert call(event).json == dict(event, service="wakaru", status="fenced")
    assert call(event).status_code == 200
    assert call(dict(event, cutoff_us=cutoff + 1)).status_code == 409
    restarted = PrivacyStore(tmp_path / "privacy.sqlite")
    service = BoundedService(restarted)
    with pytest.raises(PrivacyStop):
        service.analyze(raw, **bound)
    # Older events acknowledge without moving the durable watermark backwards.
    second = dict(event, epoch=2, cutoff_us=cutoff + 1)
    assert call(second).status_code == 200
    assert call(event).status_code == 200
    with restarted.connection() as db:
        assert db.execute("SELECT epoch,cutoff_us FROM invalidation_scopes").fetchone() == (2, cutoff + 1)
    # A newly captured, fully validated context can be interpreted after the fence.
    time.sleep(2.1)
    fresh, fresh_bound = fixture()
    assert service.analyze(json.dumps(fresh), **fresh_bound)["action"] == "review"
    # Invalidation during interpretation fences the final persistence, including
    # a restarted service. No late plan can resurrect the old input.
    another, binding = fixture()
    another["buyer_snapshot_hash"] = "f" * 64
    def invalidate_during_selection(facts, statements):
        newer = dict(event, epoch=3, cutoff_us=int(time.time() * 1000000))
        assert call(newer).status_code == 200
        return select_assistance(facts, statements)
    racing = BoundedService(restarted, selector=invalidate_during_selection)
    with pytest.raises(PrivacyStop):
        racing.analyze(json.dumps(another), **binding)
    # The generic C5 store remains independently privacy-fenced.
    assert client.post("/internal/integrations/analyze", data=raw, headers={"Authorization": sign(grant, raw)}).status_code == 422
