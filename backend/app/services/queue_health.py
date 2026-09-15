"""Operational RQ health, using heartbeats rather than container status.

Only aggregate counts/ages leave this module. Never emit job payloads, ids,
customer data or connection strings. API callers already require service auth.
"""
from datetime import datetime, timezone

from rq import Queue, Worker
from rq.job import Job
from rq.exceptions import NoSuchJobError

from .job_queue import ANALYZE_QUEUE_NAME

HEARTBEAT_MAX_AGE_SECONDS = 180
QUEUE_MAX_AGE_SECONDS = 300


def _age(value, now):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value).total_seconds()))


def queue_health(connection, now=None):
    now = now or datetime.now(timezone.utc)
    queue = Queue(ANALYZE_QUEUE_NAME, connection=connection)
    workers = Worker.all(connection=connection, queue=queue)
    live = sum(1 for worker in workers if (age := _age(worker.last_heartbeat, now)) is not None and age <= HEARTBEAT_MAX_AGE_SECONDS)
    queued = queue.count
    oldest = None
    ids = queue.get_job_ids(offset=0, length=1)
    if ids:
        try:
            oldest = _age(Job.fetch(ids[0], connection=connection).enqueued_at, now)
        except NoSuchJobError:
            pass
    healthy = live > 0 and (queued == 0 or (oldest is not None and oldest <= QUEUE_MAX_AGE_SECONDS))
    return {"healthy": healthy, "live_workers": live, "queued_jobs": queued, "oldest_queued_seconds": oldest}
