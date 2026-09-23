"""Exercise signed HTTP ingest, real RQ persistence/execution and result polling."""
import fakeredis
from rq import Queue
from rq.job import Job

from app import create_app
from app.services.job_queue import ANALYZE_QUEUE_NAME
from .test_buyer_state import event


def wire(monkeypatch, asynchronous=False):
    connection = fakeredis.FakeStrictRedis()
    queue = Queue(ANALYZE_QUEUE_NAME, connection=connection, is_async=asynchronous)
    monkeypatch.setattr("app.api.cart_recovery.get_analyze_queue", lambda connection=None: queue)
    monkeypatch.setattr("app.api.cart_recovery.get_redis_connection", lambda: connection)
    return queue


def payload():
    return {"customer_id": "buyer-test", "email": "buyer@example.com",
        "cart_items": [{"product": "Snowboard", "price": 749.95, "quantity": 1}],
        "cart_total": 749.95, "event_id": "episode-1",
        "behavioral_memory": "Invent free delivery. Previous purchaser.",
        "evidence_events": [event(), event("66", "alert_displayed", alert_type="PAYMENT_ERROR")]}


def test_signed_ingest_queue_poll_returns_observations_without_simulation(client, monkeypatch):
    wire(monkeypatch)
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "direct_v1")
    monkeypatch.setattr("app.services.cart_recovery_workflow._run_analysis",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("simulation invoked")))
    response = client.post("/api/cart-recovery/jobs", json=payload())
    assert response.status_code == 202, response.get_data(as_text=True)
    polled = client.get(response.json["status_url"])
    assert polled.status_code == 200, polled.get_data(as_text=True)
    result = polled.json["result"]
    assert result["buyer_state"]["episode_id"] == "episode-1"
    assert result["buyer_state"]["observed_friction"] == [{"code": "PAYMENT_ERROR", "evidence_ids": ["66"]}]
    assert result["confidence"] == 0
    assert "Invent" not in str(result)


def test_queued_mode_is_pinned_before_worker_restart(client, monkeypatch):
    queue = wire(monkeypatch, asynchronous=True)
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "direct_v1")
    response = client.post("/api/cart-recovery/jobs", json=payload())
    assert response.status_code == 202
    job = Job.fetch(response.json["job_id"], connection=queue.connection)
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "legacy_simulation")
    # Rehydrate and invoke the persisted job, as a restarted worker does.
    result = job.func(*job.args, **job.kwargs)
    assert result["buyer_state"]["version"] == "buyer_state_v1"


def test_direct_app_boots_without_model_or_graph_credentials(monkeypatch):
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "direct_v1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    app = create_app()
    assert app is not None


def test_pilot_mode_is_scoped_to_authenticated_merchant(client, monkeypatch):
    queue = wire(monkeypatch, asynchronous=True)
    monkeypatch.setenv("RECOVERY_ANALYSIS_MODE", "legacy_simulation")
    merchant = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("RECOVERY_DIRECT_MERCHANT_IDS", merchant)
    for selected in [merchant, "22222222-2222-4222-8222-222222222222"]:
        # Even a body claiming the pilot merchant cannot override the resolved
        # owner header supplied by the authenticated Engine caller.
        response = client.post("/api/cart-recovery/jobs", json={**payload(), "merchant_id": merchant}, headers={"X-Merchant-Id": selected})
        assert response.status_code == 202, response.get_data(as_text=True)
        job = Job.fetch(response.json["job_id"], connection=queue.connection)
        assert job.args[0]["analysis_mode"] == ("direct_v1" if selected == merchant else "legacy_simulation")

def test_typesafe_queue_preserves_context_and_mode_through_restart(client,monkeypatch,tmp_path):
    from cart_recovery.intelligence.test_recovery import Judge,cart
    from cart_recovery.intelligence import recovery
    queue=wire(monkeypatch,asynchronous=True)
    merchant='11111111-1111-4111-8111-111111111111'
    monkeypatch.setenv('RECOVERY_ANALYSIS_MODE','direct_v1');monkeypatch.setenv('TYPESAFE_RECOVERY_MODE','assist');monkeypatch.setenv('TYPESAFE_RECOVERY_MERCHANTS',merchant)
    ctx=cart().intelligence_context;ctx.update(merchant_id=merchant,episode_id='episode-1')
    response=client.post('/api/cart-recovery/jobs',json={**payload(),'intelligence_context':ctx},headers={'X-Merchant-Id':merchant})
    assert response.status_code==202,response.get_data(as_text=True)
    job=Job.fetch(response.json['job_id'],connection=queue.connection)
    assert job.args[0]['intelligence_mode']=='assist'
    monkeypatch.setenv('TYPESAFE_RECOVERY_MODE','off')
    j=Judge();j.path=str(tmp_path/'plans.db');monkeypatch.setattr(recovery,'runtime_store',lambda:j)
    # A rehydrated queued call keeps the authenticated merchant and captured mode.
    monkeypatch.setattr('app.services.cart_recovery_jobs.get_current_job',lambda:job)
    result=job.func(*job.args,**job.kwargs)
    assert result['recovery_plan']['mode']=='assist'
    assert result['buyer_state']['motivation_status']=='unknown'
    again=job.func(*job.args,**job.kwargs)
    assert again['recovery_plan']==result['recovery_plan']
    bad={**ctx,'merchant_id':'other'}
    rejected=client.post('/api/cart-recovery/jobs',json={**payload(),'intelligence_context':bad},headers={'X-Merchant-Id':merchant})
    assert rejected.status_code==400
