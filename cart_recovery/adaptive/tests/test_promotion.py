"""Real local HTTP authority and journal fixtures; no model/provider credentials."""

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from cart_recovery.adaptive import promotion
from cart_recovery.adaptive.journal import Conflict

from .fixtures import fixture
from .test_planner import setup as _planner_setup

setup = _planner_setup


@pytest.fixture
def authority(monkeypatch):
    state = {"revision": 1, "status": 200, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            state["calls"] += 1
            state["body"] = body
            key = hashlib.sha256(raw).hexdigest()
            prefix = f"{self.headers['X-AF07-Timestamp']}\nPOST\n{promotion.PATH}\n{self.headers['X-Merchant-Id']}\n{key}\n".encode()
            assert hmac.compare_digest(
                self.headers["X-AF07-Signature"],
                hmac.new(b"fixture", prefix + raw, hashlib.sha256).hexdigest(),
            )
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "release_sha256": body["release_sha256"],
                        "global_revision": state["revision"],
                        "tenant_revision": 1,
                        "request_sha256": key,
                        "valid_until": int(time.time()) + 3,
                    }
                ).encode()
            )

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for k, v in {
        "ENVIRONMENT": "local",
        "RR02_ENFORCED": "true",
        "RR02_PROCESS": "wakaru-semantic",
        "RR02_RELEASE_SHA256": "a" * 64,
        "RR02_AUTHORITY_SECRET": "fixture",
        "RR02_AUTHORITY_URL": f"http://127.0.0.1:{server.server_port}",
    }.items():
        monkeypatch.setenv(k, v)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_promotion_fresh_http_authority_and_artifact(authority):
    journal = SimpleNamespace(
        workflow="planning", deployment="b" * 64, policy={"fixture": True}
    )
    result = promotion.permit("11111111-1111-4111-8111-111111111111", journal)
    assert result[1:3] == (1, 1)
    assert authority["body"]["artifact_sha256"] == promotion.artifact_hash()
    assert authority["body"]["binding"]["deployment_sha256"] == journal.deployment
    authority["revision"] = 2
    assert promotion.permit("11111111-1111-4111-8111-111111111111", journal) != result
    authority["status"] = 503
    with pytest.raises(Conflict):
        promotion.permit("11111111-1111-4111-8111-111111111111", journal)


def test_promotion_change_during_model_call_preserves_cost_and_holds(setup, authority):
    import httpx

    from .fixtures import answer

    def response(req):
        authority["revision"] += 1
        return httpx.Response(200, json=answer(req))

    planner, calls = setup(response)
    body = fixture()
    with pytest.raises(Conflict, match="promotion_authority_changed"):
        promotion.evaluate(
            planner, body, body["context"]["merchant_id"], "promotion-race"
        )
    assert len(calls) == 1
    with planner.journal.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM af6_attempts WHERE environment=%s AND charged>0",
                (planner.journal.environment,),
            ).fetchone()[0]
            == 1
        )
    authority["status"] = 409
    with pytest.raises(Conflict):
        promotion.evaluate(
            planner, body, body["context"]["merchant_id"], "promotion-race"
        )
    assert len(calls) == 1  # cached decisions cannot bypass a stopped grant


def test_promotion_environment_typo_and_downgrade(monkeypatch):
    for value in ("Production", "prod", "local "):
        monkeypatch.setenv("ENVIRONMENT", value)
        with pytest.raises(Conflict):
            promotion.required()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RR02_ENFORCED", "false")
    assert promotion.required()
    monkeypatch.setenv("ADAPTIVE_SEMANTIC_ENVIRONMENT", "local")
    with pytest.raises(Conflict):
        promotion.required()


def test_promotion_maintenance_needs_no_model_or_grant(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADAPTIVE_SEMANTIC_DATABASE_URL", "fixture-only")
    monkeypatch.delenv("ADAPTIVE_SEMANTIC_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RR02_RELEASE_SHA256", raising=False)
    journal = promotion.MaintenanceJournal()
    assert journal.environment == "production"
    assert not hasattr(journal, "client")
