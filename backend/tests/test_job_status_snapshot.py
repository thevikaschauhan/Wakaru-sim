"""Real Redis/RQ completion races in the authenticated job polling route."""

import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis import Redis
from rq import Queue, Retry
from rq.job import Job


@pytest.mark.parametrize("mode", [None, "legacy_simulation", "direct_v1"])
def test_job_poll_keeps_status_and_metadata_in_one_snapshot(client, monkeypatch, mode):
    url = os.environ.get("WAKARU_TEST_REDIS_URL")
    if not url:
        pytest.skip("WAKARU_TEST_REDIS_URL unset")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    connection = Redis.from_url(url)
    scope = "status-snapshot-" + uuid4().hex
    queue = Queue(scope, connection=connection)
    worker_name = scope + "-worker"
    merchant = "11111111-1111-4111-8111-111111111111"
    root = Path(__file__).resolve().parents[2]
    body = {
        "customer_id": "fixture",
        "customer_name": "Fixture Shopper",
        "email": "fixture@example.test",
        "cart_items": [],
        "cart_total": 0,
        "intelligence_mode": "off",
    }
    if mode is not None:
        body["analysis_mode"] = mode
    try:
        job = queue.enqueue(
            "app.services.cart_recovery_jobs.run_analysis_job",
            body,
            job_id=scope + "-job",
            meta={"merchant_id": merchant},
            retry=Retry(max=2),
            failure_ttl=0,
            description="job status snapshot fixture",
        )
        original = connection.hget(job.key, "data")
        get_status = Job.get_status
        interleaved = []
        terminal = "finished" if mode == "direct_v1" else "failed"

        def finish_after_metadata_fetch(snapshot, refresh=True):
            if snapshot.id == job.id and not interleaved:
                interleaved.append(True)
                assert snapshot.meta == {"merchant_id": merchant}
                # Run the actual pure/retired job in a separate RQ process
                # after the web request fetched its metadata, before status.
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if k in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}
                }
                env.update(
                    PYTHONPATH=os.pathsep.join((str(root / "backend"), str(root))),
                    PYTHONDONTWRITEBYTECODE="1",
                    SENTRY_DSN="",
                )
                script = """
import sys
from pathlib import Path
from redis import Redis
from rq import Worker
import app.services.cart_recovery_jobs as jobs
assert Path(jobs.__file__).resolve() == Path(sys.argv[4]).resolve()
Worker([sys.argv[2]], connection=Redis.from_url(sys.argv[1]),
       name=sys.argv[3], log_job_description=False).work(burst=True, with_scheduler=False)
"""
                worker = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        url,
                        queue.name,
                        worker_name,
                        str(root / "backend/app/services/cart_recovery_jobs.py"),
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert worker.returncode == 0, worker.stdout + worker.stderr
                assert get_status(Job.fetch(job.id, connection=connection)) == terminal
            return get_status(snapshot, refresh=refresh)

        monkeypatch.setattr(Job, "get_status", finish_after_metadata_fetch)
        monkeypatch.setattr(
            "app.api.cart_recovery.get_redis_connection", lambda: connection
        )
        path = "/api/cart-recovery/jobs/" + job.id
        wrong = client.get(
            path, headers={"X-Merchant-Id": "22222222-2222-4222-8222-222222222222"}
        )
        assert wrong.status_code == 404 and not interleaved
        headers = {"X-Merchant-Id": merchant}
        first = client.get(path, headers=headers)
        assert first.status_code == 200 and interleaved

        def assert_terminal(value):
            assert value["status"] == terminal, value
            if terminal == "failed":
                assert value["error"] == "Analysis failed (LegacySimulationRetired)", (
                    value
                )
                assert value["progress"]["stage"] == "analysis_blocked"
            else:
                assert value["progress"]["stage"] == "buyer_state_completed", value
                assert value["result"]["buyer_state"]["version"] == "buyer_state_v1"
            assert "merchant_id" not in value["progress"]

        # A racing first poll may retain its queued snapshot or observe the
        # terminal fields together, but cannot combine status with stale meta.
        if first.json["status"] == "queued":
            assert first.json["progress"] == {} and "result" not in first.json
        else:
            assert_terminal(first.json)
        assert_terminal(client.get(path, headers=headers).json)
        assert connection.hget(job.key, "data") == original
        if terminal == "failed":
            assert connection.hget(job.key, "retries_left") == b"0"
            assert connection.ttl(job.key) == -1
            assert connection.zscore("rq:scheduled:" + queue.name, job.id) is None
    finally:
        # Only this test's queue/job/worker keys and set memberships are owned.
        keys = list(connection.scan_iter(match="*" + scope + "*"))
        if keys:
            connection.delete(*keys)
        connection.srem("rq:queues", queue.key)
        connection.srem("rq:workers", "rq:worker:" + worker_name)
        connection.close()
