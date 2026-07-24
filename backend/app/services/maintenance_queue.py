"""Maintenance queue + self-perpetuating sweep scheduler (issue #72, TDD §3.4).

The orphan-graph sweep (``zep_graph_sweeper``) runs as a recurring RQ job on a
new ``maintenance`` queue, drained by the same worker as ``analyze`` (analyze
listed first, so a paid analysis always dequeues ahead of a sweep). Scheduling
is the revision-2/3 design, proven against real Redis + the pinned RQ version:

- **Unique occurrence ids** (``zep-graph-sweep-<hex>``): no id is ever reused,
  so re-enqueueing can never overwrite a running job's hash and ``result_ttl=0``
  cleanup can only delete the finished occurrence's own record (the r1 failure).
- **Singleton execution lock** ``zep:sweep:lock`` via ``SET NX EX`` (lease >
  job timeout). A duplicate occurrence that fails to acquire exits without
  sweeping or rescheduling — declining to reschedule is what collapses duplicate
  chains back to one. Release is an **atomic Lua compare-and-delete only** (the
  r2 GET+DELETE had a lease-expiry race that could kill a successor's lock).
- **Chain-liveness marker** ``zep:sweep:next`` (``EX = 3 × interval``): while an
  occurrence runs the marker holds *that* occurrence's id (its scheduler set it).
  The lock holder advances it to the next id + schedules the next occurrence in
  its ``finally``; an ``on_failure`` callback re-seeds it via compare-and-set
  (claim when absent OR still equal to the failed occurrence's id) when the
  occurrence failed but its ``finally`` could not advance the chain; and a boot
  reconciler re-seeds it (``SET NX``) at worker start. RQ 1.16 runs ``on_failure``
  inside the (surviving) work-horse handler, *not* on a hard SIGKILL/OOM — that
  case is caught by the boot reconciler and, definitively, the Sentry Cron miss.
- **Independent watcher:** the sweep body is wrapped in a Sentry Cron Monitor
  check-in, so a missed occurrence alerts from outside the worker — no liveness
  property is attested solely by the mechanism whose death it must detect.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from uuid import uuid4

from redis.exceptions import RedisError
from rq import Queue, get_current_job
from rq.job import Callback
from sentry_sdk.crons import MonitorStatus, capture_checkin

from .job_queue import FAILURE_TTL_SECONDS, get_redis_connection
from .zep_graph_sweeper import (
    sweep_interval_minutes,
    sweep_max_deletes,
    sweep_orphan_graphs,
    sweep_page_size,
    sweep_ttl_hours,
    sweep_dry_run,
)

logger = logging.getLogger("mirofish.cart_recovery")

MAINTENANCE_QUEUE_NAME = "maintenance"
SWEEP_MONITOR_SLUG = "zep-graph-sweep"

# Per-occurrence job timeout (TDD §3.4). The lock lease must exceed it so the
# lock cannot expire under a normally-running (or timing-out) occurrence.
SWEEP_JOB_TIMEOUT = 300
LOCK_TTL_SECONDS = 360

_LOCK_KEY = "zep:sweep:lock"
_NEXT_KEY = "zep:sweep:next"

# Atomic compare-and-delete: delete the lock only if we still own it. A bare
# GET+DELETE could delete a successor's lock if our lease expired in between.
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# Atomic compare-and-set for the chain-liveness marker, used by the on_failure
# re-seed. Claim ``zep:sweep:next`` (set it to a fresh next id, ARGV[2], with
# ARGV[3] TTL) only when the marker is ABSENT *or* still equals the failed
# occurrence's own id (ARGV[1]) — i.e. the chain has not advanced past the
# occurrence that just died. A plain ``SET NX`` cannot express this: while an
# occurrence runs, the marker already holds that occurrence's own id (the prior
# occurrence set it when scheduling this one), so ``SET NX`` would find the key
# present and silently decline to re-seed, killing the chain (the r2 regression).
# When the ``finally`` already rescheduled (marker now holds the NEXT id), this
# no-ops — the re-seed stays idempotent.
_RESEED_MARKER_LUA = (
    "local cur = redis.call('get', KEYS[1]) "
    "if cur == false or cur == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 "
    "else return 0 end"
)


def get_maintenance_queue(connection=None):
    """Return the ``maintenance`` RQ ``Queue``, or ``None`` if Redis is
    unconfigured (mirrors ``job_queue.get_analyze_queue``)."""
    conn = connection if connection is not None else get_redis_connection()
    if conn is None:
        return None
    return Queue(MAINTENANCE_QUEUE_NAME, connection=conn)


def _new_occurrence_id() -> str:
    return f"zep-graph-sweep-{uuid4().hex}"


def _marker_ttl_seconds() -> int:
    return 3 * sweep_interval_minutes() * 60


def _enqueue_occurrence(queue: Queue, occurrence_id: str, delay_minutes: int) -> None:
    """Schedule one sweep occurrence ``delay_minutes`` out with a never-reused id.

    ``result_ttl=0`` discards the finished record immediately (safe now that ids
    are unique); ``on_failure`` re-seeds the chain when an occurrence fails and
    its own ``finally`` could not reschedule (see ``reseed_chain_on_failure``).
    """
    queue.enqueue_in(
        timedelta(minutes=delay_minutes),
        run_sweep_occurrence,
        job_id=occurrence_id,
        job_timeout=SWEEP_JOB_TIMEOUT,
        result_ttl=0,
        failure_ttl=FAILURE_TTL_SECONDS,
        on_failure=Callback(reseed_chain_on_failure),
    )


# --------------------------------------------------------------------------
# The sweep occurrence (the RQ job target — importable by path)
# --------------------------------------------------------------------------

def _run_sweep_with_checkin():
    """Run the sweep wrapped in a Sentry Cron Monitor check-in (in_progress →
    ok on success, error on raise). The missed-check-in alert is raised by
    Sentry's own infrastructure, so sweep death is detected by something the
    sweep cannot take down with it (TDD §3.2 step 7)."""
    check_in_id = capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG, status=MonitorStatus.IN_PROGRESS
    )
    started = time.monotonic()
    try:
        stats = sweep_orphan_graphs(
            dry_run=sweep_dry_run(),
            ttl_hours=sweep_ttl_hours(),
            page_size=sweep_page_size(),
            max_deletes=sweep_max_deletes(),
        )
    except Exception:
        capture_checkin(
            monitor_slug=SWEEP_MONITOR_SLUG,
            check_in_id=check_in_id,
            status=MonitorStatus.ERROR,
            duration=time.monotonic() - started,
        )
        raise
    capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        check_in_id=check_in_id,
        status=MonitorStatus.OK,
        duration=time.monotonic() - started,
    )
    return stats


def run_sweep_occurrence():
    """RQ job body for one sweep occurrence.

    (a) acquire the singleton lock (first action); a duplicate occurrence that
    cannot acquire exits WITHOUT sweeping or rescheduling; (b) run the sweep
    inside the cron check-in; (c) in ``finally`` release the lock via atomic Lua
    compare-and-delete only, and — if we still held it — schedule the next
    occurrence + refresh the liveness marker.
    """
    connection = get_redis_connection()
    if connection is None:
        logger.warning("zep_sweep: Redis unavailable; occurrence cannot run")
        return

    # No connection arg: RQ reads the job's connection from the execution stack
    # during a real run, and returns None when called outside a job context.
    job = get_current_job()
    occurrence_id = job.id if job is not None else _new_occurrence_id()

    acquired = connection.set(_LOCK_KEY, occurrence_id, nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        logger.info(
            "zep_sweep: occurrence %s found the lock held; exiting without "
            "sweeping or rescheduling (duplicate-chain collapse)",
            occurrence_id,
        )
        return

    try:
        _run_sweep_with_checkin()
    finally:
        if _release_lock(connection, occurrence_id):
            _schedule_next(connection)


def _release_lock(connection, occurrence_id: str) -> bool:
    """Atomic compare-and-delete. Returns True iff we still owned the lock (so a
    successor that acquired after our lease expired is never touched)."""
    try:
        return bool(connection.eval(_RELEASE_LOCK_LUA, 1, _LOCK_KEY, occurrence_id))
    except RedisError:
        logger.warning("zep_sweep: lock release failed (Redis error)")
        return False


def _schedule_next(connection) -> None:
    """Schedule the next occurrence one interval out and refresh the liveness
    marker (unconditional ``SET`` — the lock holder owns the chain)."""
    queue = get_maintenance_queue(connection)
    if queue is None:
        return
    interval = sweep_interval_minutes()
    occurrence_id = _new_occurrence_id()
    try:
        _enqueue_occurrence(queue, occurrence_id, interval)
        connection.set(_NEXT_KEY, occurrence_id, ex=_marker_ttl_seconds())
    except RedisError:
        logger.warning("zep_sweep: failed to schedule the next occurrence (Redis error)")


def reseed_chain_on_failure(job, connection, *exc_info) -> None:
    """RQ ``on_failure`` callback: re-seed the chain when an occurrence fails.

    The failure callback runs inside the work-horse's ``except`` handler (or, for
    an in-process worker, in that same process) — RQ 1.16 does **not** invoke it
    when the horse is SIGKILLed/OOM-killed, so a hard horse death is healed by
    the boot reconciler + Sentry Cron monitor, not here.

    The claim is a compare-and-set, not a bare ``SET NX``: while an occurrence
    runs, ``zep:sweep:next`` already holds *that occurrence's own id* (the prior
    occurrence set it when scheduling this one). If the occurrence's ``finally``
    could not advance the chain (its lock was lost/stolen or expired, so
    ``_schedule_next`` never ran) the marker still equals the failing id — a
    ``SET NX`` would see the key present and decline, leaving the chain dead (the
    r2 regression). So we claim when the marker is absent OR still equals the
    failed occurrence's own id, and no-op when the ``finally`` already advanced it
    to the next id (keeping the re-seed idempotent).
    """
    try:
        failed_id = job.id if job is not None else ""
        occurrence_id = _new_occurrence_id()
        claimed = connection.eval(
            _RESEED_MARKER_LUA, 1, _NEXT_KEY,
            failed_id, occurrence_id, _marker_ttl_seconds(),
        )
        if not claimed:
            return
        queue = get_maintenance_queue(connection)
        if queue is None:
            return
        _enqueue_occurrence(queue, occurrence_id, sweep_interval_minutes())
        logger.info("zep_sweep: on_failure re-seeded the occurrence chain")
    except Exception:
        logger.exception("zep_sweep: on_failure re-seed failed")


def reconcile_chain(connection) -> None:
    """Boot reconciler (worker start): if the liveness marker is absent, seed
    exactly one occurrence. ``SET NX`` is the claim, so racing replicas seed
    once. Guarded so a Redis hiccup cannot crash worker boot."""
    try:
        if connection.exists(_NEXT_KEY):
            return
        interval = sweep_interval_minutes()
        occurrence_id = _new_occurrence_id()
        claimed = connection.set(
            _NEXT_KEY, occurrence_id, nx=True, ex=_marker_ttl_seconds()
        )
        if not claimed:
            return
        queue = get_maintenance_queue(connection)
        if queue is None:
            return
        _enqueue_occurrence(queue, occurrence_id, interval)
        logger.info("zep_sweep: boot reconciler seeded a fresh occurrence chain")
    except Exception:
        logger.warning("zep_sweep: boot reconciler failed; chain not seeded")
