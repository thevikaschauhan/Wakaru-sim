"""Continuous Engine request -> signed HTTP -> persisted RQ job -> native journal."""

import json
import os
from pathlib import Path
import httpx
import pytest
from rq.job import Job
from .test_buyer_state_http import wire
from cart_recovery.intelligence import recovery
from cart_recovery.intelligence.typesafe import Store, Client


def test_completion_signed_queue_restart(client, monkeypatch, tmp_path):
    source = os.getenv("TYPESAFE_COMPLETION_REQUEST")
    if not source:
        pytest.skip("completion harness")
    body = json.loads(Path(source).read_text())
    merchant = body["intelligence_context"]["merchant_id"]
    profile = json.loads(Path("ops/typesafe/development.json").read_text())
    profile["workflows"]["recovery_intelligence"]["merchants"] = [merchant]
    deployment = tmp_path / "deployment.json"
    deployment.write_text(json.dumps(profile))
    for k, v in dict(
        TYPESAFE_PRELAUNCH="1",
        TYPESAFE_DEPLOYMENT_FILE=str(deployment),
        TYPESAFE_ENVIRONMENT="development",
        TYPESAFE_RECOVERY_MODE="enforce",
        TYPESAFE_RECOVERY_MERCHANTS=merchant,
        RECOVERY_ANALYSIS_MODE="direct_v1",
    ).items():
        monkeypatch.setenv(k, v)
    calls = []

    def transport(request):
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        req = json.loads(request.content)
        calls.append(req)
        answers = {}
        for key, q in req["questions"].items():
            if q["type"] == "noul":
                answers[key] = {
                    "type": "noul",
                    "noul": 0.95 if key == "h_payment_friction" else 0.01,
                }
            elif q["type"] == "score":
                chosen = 2 if key == "a_checkout_assistance" else 0
                answers[key] = {
                    "type": "score",
                    "score": chosen,
                    "confidence": 0.99,
                    "probabilities": {
                        str(i): float(i == chosen) for i in range(len(q["criteria"]))
                    },
                    "legend": {str(i): str(v) for i, v in enumerate(q["criteria"])},
                }
            else:
                selected = "9999" if key.startswith("e_") else "unsupported"
                if selected not in q["criteria"]:
                    selected = (
                        "none" if "none" in q["criteria"] else next(iter(q["criteria"]))
                    )
                answers[key] = {
                    "type": "choice",
                    "choice": selected,
                    "confidence": 0.99,
                    "probabilities": {k: float(k == selected) for k in q["criteria"]},
                }
        return httpx.Response(
            200,
            json={
                "model": profile["approved_models"][0],
                "answers": answers,
                "usage": {"cost": 0.0001},
            },
        )

    provider = Client(
        key="synthetic",
        approved_models=profile["approved_models"],
        transport=httpx.MockTransport(transport),
    )
    journal = tmp_path / "judgments.sqlite"
    monkeypatch.setattr(recovery, "runtime_store", lambda: Store(journal, provider))
    queue = wire(monkeypatch, asynchronous=True)
    response = client.post(
        "/api/cart-recovery/jobs", json=body, headers={"X-Merchant-Id": merchant}
    )
    assert response.status_code == 202, response.json
    job = Job.fetch(response.json["job_id"], connection=queue.connection)
    monkeypatch.setattr("app.services.cart_recovery_jobs.get_current_job", lambda: job)
    result = job.func(*job.args, **job.kwargs)
    assert result["recovery_plan"]["action"] == "checkout_assistance"
    assert result["reason_category"] == "unknown"
    count = len(calls)
    # Rehydrate both the persisted job and the SQLite store on every execution.
    restarted = Job.fetch(response.json["job_id"], connection=queue.connection)
    again = restarted.func(*restarted.args, **restarted.kwargs)
    assert again["recovery_plan"] == result["recovery_plan"] and len(calls) == count
    Path(os.environ["TYPESAFE_SYNTHETIC_INSIGHT"]).write_text(json.dumps(result))
    profile["workflows"]["recovery_intelligence"]["mode"] = "off"
    deployment.write_text(json.dumps(profile))
    held = restarted.func(*restarted.args, **restarted.kwargs)
    assert held["recovery_plan"]["action"] == "review" and len(calls) == count
    assert held["buyer_state"] == result["buyer_state"]
    provider.close()
