from datetime import datetime, timedelta, timezone
import fakeredis
from rq import Queue, Worker
from app.services.queue_health import queue_health


def test_missing_stale_and_healthy_workers():
    connection=fakeredis.FakeRedis()
    connection.client_list=lambda: [{"name":"health-test","addr":"127.0.0.1:1234"}]
    now=datetime.now(timezone.utc)
    assert not queue_health(connection,now)["healthy"]
    worker=Worker(["analyze"],connection=connection,name="health-test")
    worker.register_birth()
    assert queue_health(connection,now)["healthy"]
    connection.hset(worker.key,"last_heartbeat",(now-timedelta(seconds=181)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert not queue_health(connection,now)["healthy"]


def test_oldest_queue_age_is_unhealthy_even_with_a_worker():
    connection=fakeredis.FakeRedis()
    connection.client_list=lambda: [{"name":"health-test","addr":"127.0.0.1:1234"}]
    worker=Worker(["analyze"],connection=connection,name="health-test")
    worker.register_birth()
    queue=Queue("analyze",connection=connection)
    job=queue.enqueue("builtins.len",[1])
    now=datetime.now(timezone.utc)
    job.enqueued_at=now-timedelta(seconds=301)
    job.save()
    health=queue_health(connection,now)
    assert health=={"healthy":False,"live_workers":1,"queued_jobs":1,"oldest_queued_seconds":301}


def test_queue_health_requires_auth(app):
    assert app.test_client().get('/api/cart-recovery/queue-health').status_code==401


def test_queue_health_unconfigured(client,monkeypatch):
    monkeypatch.delenv('REDIS_URL',raising=False)
    response=client.get('/api/cart-recovery/queue-health')
    assert response.status_code==503
    assert response.get_json()["error"]=="queue_unconfigured"
