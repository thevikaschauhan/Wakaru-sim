"""Signed jobs retain paid failures and a held plan across worker retries."""

import json

import httpx
import pytest
from rq.job import Job

from cart_recovery.intelligence import recovery
from cart_recovery.intelligence.typesafe import RESERVATION, Client, Store
from .test_buyer_state_http import wire
from .test_typesafe_review_intake import MERCHANT, request_body


@pytest.mark.parametrize("oversized,cost", [(True, 0.0001), (False, 0.02)])
def test_paid_failure_holds_signed_job_after_restart(
    client, monkeypatch, tmp_path, oversized, cost
):
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "direct_v1")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "enforce")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MERCHANTS", MERCHANT)
    calls = []

    def transport(request):
        questions = json.loads(request.content)["questions"]
        calls.append(questions)
        # Recovery asks hypotheses first; the provider returns an invalid noul.
        first = next(iter(questions))
        assert questions[first]["type"] == "noul"
        answers = {key: {"type": q["type"]} for key, q in questions.items()}
        answers[first]["noul"] = 10**400 if oversized else None
        return httpx.Response(200, content=json.dumps({
            "model": "typesafe/jev-1.13-20260917", "answers": answers,
            "usage": {"cost": cost},
        }))

    provider = Client(
        key="synthetic", approved_models=["typesafe/jev-1.13-20260917"],
        transport=httpx.MockTransport(transport),
    )
    journal = tmp_path / "judgments.sqlite"
    monkeypatch.setattr(recovery, "runtime_store", lambda: Store(journal, provider))
    try:
        queue = wire(monkeypatch, asynchronous=True)
        response = client.post(
            "/api/cart-recovery/jobs", json=request_body(),
            headers={"X-Merchant-Id": MERCHANT},
        )
        assert response.status_code == 202, response.json
        job = Job.fetch(response.json["job_id"], connection=queue.connection)
        monkeypatch.setattr("app.services.cart_recovery_jobs.get_current_job", lambda: job)
        result = job.func(*job.args, **job.kwargs)
        assert result["recovery_plan"]["action"] == "review"
        assert result["recovery_plan"]["status"] == "needs_review"
        assert len(calls) == 1
        restarted = Job.fetch(job.id, connection=queue.connection)
        monkeypatch.setattr("app.services.cart_recovery_jobs.get_current_job", lambda: restarted)
        again = restarted.func(*restarted.args, **restarted.kwargs)
        assert again["recovery_plan"] == result["recovery_plan"]
        assert len(calls) == 1
        with Store(journal, provider).connection() as db:
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
            saved = db.execute("SELECT result FROM results").fetchall()
            assert len(saved) == 1
            attempt = json.loads(saved[0][0])
            assert attempt["error_class"] == "schema_failure"
            assert attempt["cost"] == cost
            assert db.execute("SELECT count(*) FROM judgments").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM selected").fetchone()[0] == 0
            assert [r[0] for r in db.execute("SELECT spent FROM budgets")] == pytest.approx(
                [max(RESERVATION, cost)] * 3
            )
    finally:
        provider.close()
