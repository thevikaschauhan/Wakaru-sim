"""Maintenance queue + self-perpetuating sweep scheduler (issue #72, TDD §3.4).

The orphan-graph sweep (``zep_graph_sweeper``) runs as a recurring RQ job on a
new ``maintenance`` queue, drained by the same worker as ``analyze`` (analyze
listed first, so a paid analysis always dequeues ahead of a sweep). Scheduling
is the revision-2/3 design, proven against real Redis + the pinned RQ version:

- **Unique occurrence ids** (``zep-graph-sweep-<hex>``): no id is ever reused,
  so re-enqueueing can never overwrite a running job's hash and ``result_ttl=0``
  cleanup can only delete the finished occurrence's own record (the r1 failure).
- **Generation ownership via the marker** (the duplicate-chain collapse — F1):
  ``zep:sweep:next`` names the ONE canonical next occurrence. An occurrence may
  sweep and advance the chain ONLY if its own job id equals the marker (checked
  atomically at entry). A non-canonical occurrence — a duplicate, whether it
  overlaps a sibling or (the r-review bug) runs *serially* after one on a single
  worker — exits WITHOUT sweeping or rescheduling. The lock alone could not do
  this: with one worker, queued occurrences run one at a time, so a
  lock-freed-then-reacquired duplicate would sweep and perpetuate a second chain.
- **Singleton execution lock** ``zep:sweep:lock`` via ``SET NX EX`` (lease >
  job timeout) is now only a **secondary concurrency guard** across replicas for
  the sweep body, not the collapse mechanism. Release is an **atomic Lua
  compare-and-delete only** (the r2 GET+DELETE had a lease-expiry race that could
  kill a successor's lock).
- **Chain-liveness marker** ``zep:sweep:next`` (``EX = 3 × interval``): while an
  occurrence runs the marker holds *that* occurrence's id (its scheduler set it),
  which is exactly what makes it the canonical generation. The canonical occurrence
  enqueues its successor and then **CAS-advances** the marker from its own id to
  the successor id (advance only while it still names us). To avoid a marker that
  points at a job that was never scheduled (F3), every path enqueues FIRST and
  only then claims/advances the marker (or, when it claims first, compare-deletes
  the exact claimed id if the enqueue fails). An ``on_failure`` callback re-seeds
  the chain via compare-and-set (claim when absent OR still equal to the failed
  occurrence's id) when the occurrence failed but its ``finally`` could not advance
  the chain; and a boot reconciler re-seeds it (``SET NX``) at worker start. RQ
  1.16 runs ``on_failure`` inside the (surviving) work-horse handler, *not* on a
  hard SIGKILL/OOM — that case is caught by the boot reconciler and, definitively,
  the Sentry Cron miss.
- **Independent watcher:** the sweep body is wrapped in a Sentry Cron Monitor
  check-in that upserts a ``monitor_config`` (interval schedule + margins) so
  Sentry can detect a *missed* occurrence from outside the worker — no liveness
  property is attested solely by the mechanism whose death it must detect.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from uuid import uuid4

from redis.exceptions import RedisError
from rq import Queue, get_current_job
from rq.job import Callback, Job
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

# Atomic compare-and-delete primitive (used for both the lock release and the
# marker rollback): delete KEYS[1] only if it still equals ARGV[1]. A bare
# GET+DELETE could delete a successor's key if our value changed in between.
_COMPARE_DELETE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# Atomic compare-and-set that ADVANCES the marker: set ``zep:sweep:next`` to the
# successor id (ARGV[2], ARGV[3] TTL) only while it still equals the caller's own
# occurrence id (ARGV[1]). Used by the canonical occurrence to hand the chain to
# its successor. If another path already advanced the marker (on_failure re-seed,
# boot reconcile), this no-ops and the caller's successor is simply non-canonical
# and self-collapses — it is never an error.
_ADVANCE_MARKER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1 "
    "else return 0 end"
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

def _sweep_monitor_config() -> dict:
    """Build the Sentry Cron ``monitor_config`` from the sweep interval (F4).

    Without it Sentry has no schedule to compare check-ins against and therefore
    **cannot detect a MISSED occurrence** — the whole point of the external
    watcher. The check-in upserts/versions the monitor in code: an interval
    schedule derived from ``ZEP_SWEEP_INTERVAL_MINUTES``, a ``checkin_margin``
    grace of one interval (a starved sweep alerts within interval + grace, still
    inside the ``3 × interval`` marker TTL), and a ``max_runtime`` bound derived
    from the per-occurrence job timeout."""
    interval = sweep_interval_minutes()
    return {
        "schedule": {"type": "interval", "value": interval, "unit": "minute"},
        "checkin_margin": interval,
        "max_runtime": (SWEEP_JOB_TIMEOUT + 59) // 60,  # ceil(timeout / 60) minutes
        "timezone": "UTC",
    }


def _run_sweep_with_checkin():
    """Run the sweep wrapped in a Sentry Cron Monitor check-in (in_progress →
    ok on success, error on raise). The missed-check-in alert is raised by
    Sentry's own infrastructure, so sweep death is detected by something the
    sweep cannot take down with it (TDD §3.2 step 7). Each check-in carries the
    ``monitor_config`` so the monitor's schedule is defined in code and Sentry can
    flag a missed occurrence (F4)."""
    monitor_config = _sweep_monitor_config()
    check_in_id = capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        status=MonitorStatus.IN_PROGRESS,
        monitor_config=monitor_config,
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
            monitor_config=monitor_config,
        )
        raise
    capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        check_in_id=check_in_id,
        status=MonitorStatus.OK,
        duration=time.monotonic() - started,
        monitor_config=monitor_config,
    )
    return stats


def run_sweep_occurrence():
    """RQ job body for one sweep occurrence.

    (a) **generation disposition** (first action): compare the occurrence's id to
    the liveness marker. Only the marker-named (canonical) occurrence sweeps; a
    duplicate whose marker names another LIVE occurrence exits (collapse); a
    ``dead`` disposition — marker absent (TTL-starved) or naming a job that no
    longer exists (phantom) — RE-SEEDS one fresh chain in-band (F3) rather than
    dying silently, then exits without sweeping. (b) The canonical occurrence
    takes the singleton lock as a secondary concurrency guard across replicas;
    (c) runs the sweep inside the cron check-in; (d) in ``finally`` releases the
    lock (best-effort) and, **on sweep SUCCESS regardless of the release
    outcome** (F2), schedules the successor + CAS-advances the marker.
    """
    connection = get_redis_connection()
    if connection is None:
        logger.warning("zep_sweep: Redis unavailable; occurrence cannot run")
        return

    # No connection arg: RQ reads the job's connection from the execution stack
    # during a real run, and returns None when called outside a job context.
    job = get_current_job()
    occurrence_id = job.id if job is not None else _new_occurrence_id()

    disposition, marker_value = _generation_disposition(connection, occurrence_id)
    if disposition == "duplicate":
        logger.info(
            "zep_sweep: occurrence %s is a duplicate (marker names live %s); "
            "exiting without sweeping or rescheduling (chain collapse)",
            occurrence_id, marker_value,
        )
        return
    if disposition == "declined":
        # Redis error on the ownership read — fail closed: neither sweep nor
        # re-seed (a mistaken action is worse than a skip the Sentry monitor
        # will surface).
        return
    if disposition == "dead":
        # Chain lost: marker absent (queued past the 3xinterval TTL) or naming a
        # job that no longer exists (phantom from a crash). Re-seed exactly one
        # fresh chain in-band (F3) — the atomic claim collapses racing occurrences
        # to one — and exit without sweeping; the fresh successor sweeps next.
        try:
            if _reseed_if_marker_is(connection, marker_value):
                logger.info(
                    "zep_sweep: occurrence %s found the chain dead (marker=%s); "
                    "re-seeded a fresh chain", occurrence_id, marker_value,
                )
        except Exception:
            logger.exception("zep_sweep: dead-chain re-seed failed")
        return

    # disposition == "canonical": this is the one live generation — sweep.
    acquired = connection.set(_LOCK_KEY, occurrence_id, nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        logger.info(
            "zep_sweep: canonical occurrence %s found the sweep lock held by a "
            "concurrent replica; exiting without sweeping or rescheduling",
            occurrence_id,
        )
        return

    swept_ok = False
    try:
        _run_sweep_with_checkin()
        swept_ok = True
    finally:
        # Release is best-effort: the 360s lock TTL backstops a missed release,
        # and the successor runs an interval (>> lease) later. F2: schedule the
        # successor whenever the sweep SUCCEEDED, decoupled from the release
        # result — a transient Redis error on release must NOT silently end the
        # chain. On sweep FAILURE we do not schedule here; the exception
        # propagates and ``on_failure`` re-seeds.
        _release_lock(connection, occurrence_id)
        if swept_ok:
            _schedule_next(connection, occurrence_id)


def _job_exists(connection, job_id: str) -> bool:
    """Whether an RQ job hash for ``job_id`` still exists (scheduled or running).
    A scheduled occurrence's hash exists until it runs and ``result_ttl=0``
    clears it. On any uncertainty, returns True so the caller does NOT re-seed
    (avoids creating a duplicate chain)."""
    if not job_id:
        return False
    try:
        return Job.exists(job_id, connection=connection)
    except Exception:
        return True


def _generation_disposition(connection, occurrence_id: str):
    """Classify this occurrence against the chain-liveness marker (F1/F3).

    Returns ``(disposition, marker_value)`` where disposition is:
    - ``"canonical"`` — the marker names THIS occurrence: the one live generation.
    - ``"duplicate"`` — the marker names ANOTHER occurrence whose job still
      exists: a true duplicate (replica race / manual enqueue) → exit, collapse.
    - ``"dead"`` — the marker is absent (chain expired past its TTL) OR names a
      job that no longer exists (phantom from a crash): the chain is lost and
      must be re-seeded in-band.
    - ``"declined"`` — a Redis error on the read: fail closed (do nothing).
    """
    try:
        marker = connection.get(_NEXT_KEY)
    except RedisError:
        logger.warning(
            "zep_sweep: generation check failed (Redis error); occurrence declines"
        )
        return "declined", None
    if marker is None:
        return "dead", None
    if isinstance(marker, bytes):
        marker = marker.decode()
    if marker == occurrence_id:
        return "canonical", marker
    # The marker names a different occurrence. It is a real duplicate only if
    # that occurrence's job still exists; otherwise the marker is a phantom and
    # the chain is dead.
    if _job_exists(connection, marker):
        return "duplicate", marker
    return "dead", marker


def _release_lock(connection, occurrence_id: str) -> bool:
    """Atomic compare-and-delete. Returns True iff we still owned the lock (so a
    successor that acquired after our lease expired is never touched)."""
    try:
        return bool(connection.eval(_COMPARE_DELETE_LUA, 1, _LOCK_KEY, occurrence_id))
    except RedisError:
        logger.warning("zep_sweep: lock release failed (Redis error)")
        return False


def _compare_delete_marker(connection, occurrence_id: str) -> None:
    """Delete the liveness marker only if it still names ``occurrence_id``. Rolls
    back a claim whose enqueue then failed, so no marker is ever left pointing at
    a job that was never scheduled (F3)."""
    try:
        connection.eval(_COMPARE_DELETE_LUA, 1, _NEXT_KEY, occurrence_id)
    except RedisError:
        logger.warning("zep_sweep: marker rollback failed (Redis error)")


def _schedule_next(connection, occurrence_id: str) -> None:
    """Schedule the successor occurrence, then CAS-advance the liveness marker
    from our own id to the successor's — the successor becomes the one canonical
    next generation (F1).

    Enqueue happens FIRST so the marker can never name a job that was not
    scheduled (F3 phantom marker). A failure to schedule the successor is
    **RAISED, not swallowed** (F3): it fails the occurrence so ``on_failure``
    re-seeds the chain. A swallowed schedule error would let the occurrence
    succeed while the chain silently died. The CAS-advance no-ops (harmlessly) if
    the marker no longer names us — that is not a scheduling failure."""
    queue = get_maintenance_queue(connection)
    if queue is None:
        return
    interval = sweep_interval_minutes()
    successor_id = _new_occurrence_id()
    _enqueue_occurrence(queue, successor_id, interval)
    connection.eval(
        _ADVANCE_MARKER_LUA, 1, _NEXT_KEY,
        occurrence_id, successor_id, _marker_ttl_seconds(),
    )


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
        if _reseed_if_marker_is(connection, failed_id):
            logger.info("zep_sweep: on_failure re-seeded the occurrence chain")
    except Exception:
        logger.exception("zep_sweep: on_failure re-seed failed")


def _reseed_if_marker_is(connection, expected_id) -> bool:
    """Atomically claim the liveness marker when it is ABSENT or still equals
    ``expected_id``, then enqueue exactly one fresh occurrence and point the
    marker at it. Rolls back (compare-delete) the claimed id if the enqueue
    fails, so the marker never names a job that was not scheduled (F3/F4).
    Returns True iff it seeded. The claim is atomic, so racing re-seeders (on
    failure / dead-chain heal / boot reconcile) collapse to exactly one chain.

    ``expected_id`` is ``""``/``None`` to claim only an absent marker, or the
    exact id the marker must still hold (a failed occurrence's own id, or a
    phantom id naming a job that no longer exists)."""
    occurrence_id = _new_occurrence_id()
    claimed = connection.eval(
        _RESEED_MARKER_LUA, 1, _NEXT_KEY,
        expected_id or "", occurrence_id, _marker_ttl_seconds(),
    )
    if not claimed:
        return False
    queue = get_maintenance_queue(connection)
    if queue is None:
        _compare_delete_marker(connection, occurrence_id)
        return False
    try:
        _enqueue_occurrence(queue, occurrence_id, sweep_interval_minutes())
    except Exception:
        _compare_delete_marker(connection, occurrence_id)
        raise
    return True


def reconcile_chain(connection) -> None:
    """Boot reconciler (worker start): re-seed the chain unless the marker names
    a job that still EXISTS (F4). The r1 version trusted marker *presence* alone,
    so a crash between the marker claim and the enqueue left a phantom marker
    that no later boot would ever heal. Now: a healthy chain (marker present AND
    its job exists) is left alone; an absent marker OR a phantom one (names a
    non-existent job) is re-seeded via the atomic claim (racing replicas seed
    once). Guarded so a Redis hiccup cannot crash worker boot."""
    try:
        marker = connection.get(_NEXT_KEY)
        if isinstance(marker, bytes):
            marker = marker.decode()
        if marker is not None and _job_exists(connection, marker):
            return  # healthy chain — marker names a real scheduled/running job
        if _reseed_if_marker_is(connection, marker):
            logger.info("zep_sweep: boot reconciler seeded a fresh occurrence chain")
    except Exception:
        logger.warning("zep_sweep: boot reconciler failed; chain not seeded")
