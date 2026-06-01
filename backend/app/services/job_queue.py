"""Redis + RQ job-queue wiring for async cart-recovery (issue #20).

The ``/api/cart-recovery/jobs`` endpoint enqueues onto the ``analyze`` queue;
a separate Railway worker service (``backend/worker.py``) drains it. ``/analyze``
stays synchronous and does NOT touch Redis (the issue #20 bridge), so this module
is the only Redis dependency on the web tier and a missing/unreachable Redis
degrades ``/jobs`` to a 503 rather than breaking ``/analyze``.

``REDIS_URL`` / ``ANALYZE_JOB_TIMEOUT`` are read from ``os.environ`` directly
(not ``Config.<attr>``) for the same reason ``Config.validate()`` does: class
attributes freeze at import, so runtime/test env changes only apply when read
live. See ``backend/app/config.py``.
"""
from __future__ import annotations

import os

import redis
from rq import Queue

ANALYZE_QUEUE_NAME = "analyze"

# Outer safety net on the RQ job. The real bound is the workflow's internal
# ~58-min poll ceiling (cart_recovery_workflow._RUN_POLL_TIMEOUT_SECONDS = 3480s);
# this must comfortably exceed it (+ queue wait + the pre-poll/post-poll stages)
# or RQ SIGKILLs the worker mid-pipeline and orphans the OASIS subprocess.
DEFAULT_ANALYZE_JOB_TIMEOUT = 5400  # 90 min

# Keep finished results and failure metadata readable long after a run so the
# engine's poll loop can always reach a terminal job (RQ's ~500s result default
# is far shorter than an 8-17 min run + the caller's polling window).
RESULT_TTL_SECONDS = 86400   # 24h
FAILURE_TTL_SECONDS = 86400  # 24h


def analyze_job_timeout() -> int:
    """RQ ``job_timeout`` for an analyze job (env-overridable)."""
    raw = os.environ.get("ANALYZE_JOB_TIMEOUT")
    if not raw:
        return DEFAULT_ANALYZE_JOB_TIMEOUT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_ANALYZE_JOB_TIMEOUT


def get_redis_connection():
    """Return a Redis connection from ``REDIS_URL``, or ``None`` if unconfigured.

    Returning ``None`` (rather than raising) lets the web tier answer ``/jobs``
    with a clean 503 while leaving ``/analyze`` unaffected.
    """
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    return redis.Redis.from_url(url)


def get_analyze_queue(connection=None):
    """Return the ``analyze`` RQ ``Queue``, or ``None`` if Redis is unconfigured."""
    conn = connection if connection is not None else get_redis_connection()
    if conn is None:
        return None
    return Queue(ANALYZE_QUEUE_NAME, connection=conn)
