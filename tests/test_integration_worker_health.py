import json
from pathlib import Path
from cart_recovery.integration_privacy import PrivacyStore
from cart_recovery.integration_worker_health import PATH, create_worker_service_app
from test_integration_service import sign


def test_c13_authenticated_health_restart_and_no_payload(tmp_path):
    store = PrivacyStore(tmp_path / "health.sqlite")
    merchant = "11111111-1111-4111-8111-111111111111"
    grant = dict(key_id="fixture", secret="c13-fixture-" + "x" * 32, merchant_id=merchant,
                 store_id="17", audience="wakaru", operations=["POST " + PATH, "POST /internal/integrations/invalidate"])
    client = create_worker_service_app(enabled=True, grants=[grant], store=store).test_client()

    def health(raw=b"{}", authenticated=True):
        return client.post(PATH, data=raw, headers={"Authorization": sign(grant, raw, PATH)} if authenticated else {})

    assert create_worker_service_app().test_client().post(PATH).status_code == 404
    assert health(authenticated=False).status_code == 401
    assert health(b'{"store_id":"18"}').status_code == 400
    assert health(b'{}' + b' ' * 16).status_code == 400
    response = health()
    value = response.json
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert value["epoch"] == 0 and value["cleanup_state"] == "not_required"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM privacy_scopes").fetchone()[0] == 0
    event = dict(version="integration_invalidation_c12_v1", merchant_id=merchant, store_id="17", epoch=3, cutoff_us=1793059200000000)
    raw = json.dumps(event).encode()
    assert client.post("/internal/integrations/invalidate", data=raw,
                       headers={"Authorization": sign(grant, raw, "/internal/integrations/invalidate")}).status_code == 200
    restarted = create_worker_service_app(enabled=True, grants=[grant], store=PrivacyStore(tmp_path / "health.sqlite")).test_client()
    response = restarted.post(PATH, data=b"{}", headers={"Authorization": sign(grant, b"{}", PATH)})
    assert response.json["epoch"] == 3
    expected = json.loads((Path(__file__).resolve().parents[1] / "contracts/integrations/c13/health.json").read_text())["wakaru"]
    assert set(response.json) == set(expected)
    assert all(response.json[k] is False for k in ("live_ingestion_enabled", "deployment_enabled", "email_sends_enabled"))
    assert response.json["delivery"] == [] and response.json["worker_state"] == "not_required"
    assert "cutoff_us" not in response.json and "sources" not in response.json
    # A separately authorized tenant has no access to this merchant's watermark.
    foreign = dict(grant, key_id="foreign", merchant_id="22222222-2222-4222-8222-222222222222")
    other = create_worker_service_app(enabled=True, grants=[foreign], store=store).test_client()
    assert other.post(PATH, data=b"{}", headers={"Authorization": sign(foreign, b"{}", PATH)}).json["epoch"] == 0
    # Query tampering and credentials without this operation cannot read metadata.
    assert client.post(PATH + "?store_id=18", data=b"{}", headers={"Authorization": sign(grant, b"{}", PATH)}).status_code == 401
    no_health = dict(grant, operations=["POST /internal/integrations/invalidate"])
    denied = create_worker_service_app(enabled=True, grants=[no_health], store=store).test_client()
    assert denied.post(PATH, data=b"{}", headers={"Authorization": sign(grant, b"{}", PATH)}).status_code == 401
