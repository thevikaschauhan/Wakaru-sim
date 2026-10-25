"""Verify the real worker entrypoint owns the idle-retention monitor lifetime."""

import importlib.util
from pathlib import Path
import threading

import pytest


def test_worker_starts_retention_and_stops_both_monitors(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "worker.py"
    spec = importlib.util.spec_from_file_location("typesafe_review_worker", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    monkeypatch.setattr(worker, "create_app", lambda: None)
    monkeypatch.setattr(worker, "get_redis_connection", lambda: object())
    queue_stop, retention_stop = threading.Event(), threading.Event()
    started = []

    def start_queue():
        started.append("queue")
        return queue_stop

    def start_retention():
        started.append("retention")
        return retention_stop

    class TestWorker:
        def __init__(self, *args, **kwargs):
            pass

        def work(self):
            assert started == ["queue", "retention"]
            raise RuntimeError("worker stopped")

    monkeypatch.setattr("rq.Worker", TestWorker)
    monkeypatch.setattr("app.services.worker_monitor.start_queue_monitor", start_queue)
    monkeypatch.setattr(
        "cart_recovery.intelligence.maintenance.start_retention_monitor",
        start_retention,
    )
    with pytest.raises(RuntimeError, match="worker stopped"):
        worker.main()
    assert queue_stop.is_set() and retention_stop.is_set()
