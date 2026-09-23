from rq.job import Job
from .test_buyer_state_http import payload, wire


def test_queue_pins_conversion_revision_and_freezes_unavailable(
    client, monkeypatch, tmp_path
):
    from cart_recovery.intelligence.test_recovery import Judge, cart
    from cart_recovery.intelligence import recovery

    queue = wire(monkeypatch, asynchronous=True)
    merchant = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "direct_v1")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "assist")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MERCHANTS", merchant)
    monkeypatch.setenv("CONVERSION_MODEL_MERCHANTS", merchant)
    monkeypatch.setenv("CONVERSION_MODEL_ID", "a" * 64)
    ctx = cart().intelligence_context
    ctx.update(merchant_id=merchant, episode_id="episode-1")
    response = client.post(
        "/api/cart-recovery/jobs",
        json={
            **payload(),
            "intelligence_context": ctx,
            "conversion_model_id": "untrusted-body-value",
        },
        headers={"X-Merchant-Id": merchant},
    )
    assert response.status_code == 202
    job = Job.fetch(response.json["job_id"], connection=queue.connection)
    assert job.args[0]["conversion_model_id"] == "a" * 64
    monkeypatch.setenv("CONVERSION_MODEL_ID", "b" * 64)
    judge = Judge()
    judge.path = str(tmp_path / "plans.db")
    monkeypatch.setattr(recovery, "runtime_store", lambda: judge)
    monkeypatch.setattr("app.services.cart_recovery_jobs.get_current_job", lambda: job)
    first = job.func(*job.args, **job.kwargs)
    estimate = first["buyer_intelligence"]["conversion_estimate"]
    assert estimate["model_version"] == "a" * 64 and estimate["probability"] is None
    assert estimate["status"] == "not_available"
    second = job.func(*job.args, **job.kwargs)
    assert first["buyer_intelligence"] == second["buyer_intelligence"]
    assert first["recovery_plan"] == second["recovery_plan"]
    assert not first["recovery_plan"]["allow_incentive"]
