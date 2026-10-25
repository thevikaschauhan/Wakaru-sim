import copy
import hashlib
import hmac
import json
from pathlib import Path
import time

from cart_recovery.integration_service_auth import ServiceGrants
from cart_recovery.integration_service import create_service_app
from cart_recovery.integration_privacy import PrivacyStore
from test_bounded_context import fixture

ROOT = Path(__file__).resolve().parents[1]


def sign(g, body, path="/internal/integrations/analyze", at=None):
    stamp = str(int(time.time() if at is None else at))
    payload = "\n".join(("integration_service_request_v1", g["key_id"], g["audience"], g["merchant_id"], g["store_id"], "POST", path, stamp, hashlib.sha256(body).hexdigest()))
    return "Integration v1." + g["key_id"] + "." + stamp + "." + hmac.new(g["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()


def test_c11_wire_and_authority_boundaries():
    f = json.loads((ROOT / "contracts/integrations/c11/service-auth.json").read_text())
    v = ServiceGrants("wakaru", [f["grant"]])
    assert sign(f["grant"], f["body"].encode(), at=f["unix_time"]) == f["authorization"]
    assert v.verify(f["authorization"], "POST", f["path"], f["body"].encode(), f["unix_time"])["store_id"] == "17"
    for delta in (-1, 31):
        assert v.verify(f["authorization"], "POST", f["path"], f["body"].encode(), f["unix_time"] + delta) is None
    for field, value in (("store_id", "18"), ("merchant_id", "22222222-2222-2222-2222-222222222222"), ("audience", "inkwell"), ("secret", "z" * 32)):
        grant = dict(f["grant"], **{field: value})
        assert ServiceGrants(grant["audience"], [grant]).verify(f["authorization"], "POST", f["path"], f["body"].encode(), f["unix_time"]) is None
    assert v.verify(f["authorization"], "POST", f["path"], f["body"].encode() + b" ", f["unix_time"]) is None
    assert v.verify(f["authorization"], "GET", f["path"], f["body"].encode(), f["unix_time"]) is None
    assert v.verify(f["authorization"], "POST", f["path"] + "?x=1", f["body"].encode(), f["unix_time"]) is None


def test_c11_authenticated_analysis_restart_and_privacy(tmp_path):
    value, bound = fixture()
    grant = dict(key_id="fixture", secret="fixture-only-" + "x" * 32, merchant_id=bound["merchant"], store_id=bound["store"], audience="wakaru", operations=["POST /internal/integrations/analyze"])
    store = PrivacyStore(tmp_path / "privacy.sqlite")
    assert create_service_app().test_client().post("/internal/integrations/analyze").status_code == 404
    client = create_service_app(enabled=True, grants=[grant], store=store).test_client()
    body = json.dumps(value).encode()
    headers = {"Authorization": sign(grant, body), "Content-Type": "application/json"}
    assert client.post("/internal/integrations/analyze", data=body).status_code == 401
    assert client.post("/internal/integrations/analyze", data=body+b" ", headers=headers).status_code == 401
    wrong = copy.deepcopy(value)
    wrong["context"]["customer_context"]["store_id"] = "18"
    raw = json.dumps(wrong).encode()
    assert client.post("/internal/integrations/analyze", data=raw, headers={"Authorization": sign(grant, raw)}).status_code == 401
    result = client.post("/internal/integrations/analyze", data=body, headers=headers)
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert result.json["plan"]["allowed_claims"] == [value["context"]["approved_facts"][0]["text"]]
    assert result.json["plan"]["action"] == "review"
    # Recreate the application and storage owner, preserving the frozen result.
    restarted = create_service_app(enabled=True, grants=[grant], store=PrivacyStore(tmp_path / "privacy.sqlite")).test_client()
    assert restarted.post("/internal/integrations/analyze", data=body, headers=headers).json == result.json
    assert client.post("/internal/integrations/jobs", data=body, headers=headers).status_code == 401
    task = dict(version="integration_privacy_task_v1", merchant_id=bound["merchant"], store_id=bound["store"], request_id="c11-erase", subject_key=hashlib.sha256(bound["subject"].encode()).hexdigest(), privacy_generation=2, operation="erase", scope="subject", source_keys=value["source_keys"], inventory_revision="integration_c5_v1", live_ingestion_enabled=False, email_use_enabled=False)
    store.apply(bound["merchant"], bound["store"], json.dumps(task), now=time.time())
    assert restarted.post("/internal/integrations/analyze", data=body, headers=headers).status_code == 422
