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
  points at a job that was never scheduled (F3/F4), EVERY path enqueues the fresh
  occurrence FIRST and only then CAS-sets/advances the marker to it — so a hard
  process death between the two steps leaves at most a self-collapsing orphan job,
  never a phantom marker. An ``on_failure`` callback re-seeds the chain via
  compare-and-set (claim when absent OR still equal to the failed occurrence's id)
  when the occurrence failed but its ``finally`` could not advance the chain, and
  frees that occurrence's stale lock; a boot reconciler re-seeds it at worker
  start (only when the marker is absent, phantom, or names a terminal job). RQ
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
from rq.exceptions import NoSuchJobError
from rq.job import Callback, Job, JobStatus
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

# RQ job states that mean a chain occurrence is genuinely LIVE. Terminal states
# (finished/failed/stopped/canceled) are NOT live even though RQ retains their
# hashes (failure_ttl, the canceled registry), so hash-presence (``Job.exists``)
# is not a liveness test — a terminal occurrence must read as a dead chain to
# re-seed (F3 _job_is_live).
_RUNNABLE_JOB_STATUSES = frozenset(
    {JobStatus.QUEUED, JobStatus.STARTED, JobStatus.DEFERRED, JobStatus.SCHEDULED}
)

# Atomic compare-and-delete primitive (the singleton-lock release): delete
# KEYS[1] only if it still equals ARGV[1]. A bare GET+DELETE could delete a
# successor's key if our value changed in between.
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


# Every sweep occurrence's job id starts with this; the killed-horse / on_failure
# re-seed keys off it so a killed NON-sweep job (a paid analysis) never triggers a
# sweep re-seed (which, being enqueue-first, would otherwise enqueue an orphan
# occurrence before its CAS lost — wasteful churn under repeated analysis
# failures, revision-8 F2).
_OCCURRENCE_ID_PREFIX = "zep-graph-sweep-"


def _new_occurrence_id() -> str:
    return f"{_OCCURRENCE_ID_PREFIX}{uuid4().hex}"


def _is_sweep_occurrence(job) -> bool:
    """True iff ``job`` is a sweep occurrence (its id carries the occurrence
    prefix). ``None`` (a direct/testing call with no job) counts as a sweep
    re-seed; a non-sweep job (a killed paid analysis) does not."""
    return job is None or str(getattr(job, "id", "")).startswith(_OCCURRENCE_ID_PREFIX)


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
    takes the singleton lock as a secondary concurrency guard across replicas; if
    the lock is held (a stale lease from a dead prior occurrence — see the branch
    below) it still schedules the successor + advances the marker so the chain
    never goes empty, then returns without sweeping. (c) Otherwise it runs the
    sweep inside the cron check-in; (d) in ``finally`` releases the lock
    (best-effort) and, **on sweep SUCCESS regardless of the release outcome**
    (F2), schedules the successor + CAS-advances the marker.
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
        # The lock is held even though WE are the canonical occurrence. Being
        # canonical means the marker was already advanced to us, which only a
        # *prior* occurrence's completion (or a re-seed after it died) does — a
        # still-running occurrence would keep the marker on itself. So the holder
        # is a DEAD occurrence whose stale lease has not yet expired (LOCK_TTL of
        # 360s outlives the 300s interval floor). Do NOT exit empty — that would
        # kill the chain. Skip the sweep this cycle (we could not take the lock)
        # but SCHEDULE THE SUCCESSOR + advance the marker so the chain self-heals:
        # the stale lease clears before the successor runs, so it sweeps. This is
        # the robust backstop for the LOCK_TTL/interval relationship (no operator
        # config change needed); the killed-horse / on_failure path also releases
        # the dead lease so the successor need not even wait it out.
        logger.warning(
            "zep_sweep: canonical occurrence %s found the sweep lock held by a "
            "stale lease from a dead prior occurrence; not sweeping this cycle, "
            "scheduling the successor to keep the chain alive (self-heal)",
            occurrence_id,
        )
        _schedule_next(connection, occurrence_id)
        return

    swept_ok = False
    try:
        _run_sweep_with_checkin()
        swept_ok = True
    finally:
        # Release is best-effort: an atomic compare-and-delete keyed to our own id
        # (the LOCK_TTL is only a backstop for a missed release). F2: schedule the
        # successor whenever the sweep SUCCEEDED, decoupled from the release
        # result — a transient Redis error on release must NOT silently end the
        # chain. On sweep FAILURE we do not schedule here; the exception
        # propagates and ``on_failure`` re-seeds (and releases our lease).
        _release_lock(connection, occurrence_id)
        if swept_ok:
            _schedule_next(connection, occurrence_id)


def _job_is_live(connection, job_id: str) -> bool:
    """Whether the RQ job named ``job_id`` is in a RUNNABLE state — i.e. a
    genuinely live chain occurrence (queued / started / deferred / scheduled).

    Hash-presence is NOT liveness: RQ retains a FAILED or CANCELED job's hash
    (failure_ttl, the canceled registry), so ``Job.exists`` would misread such a
    terminal occurrence as a healthy duplicate and block the dead-chain re-seed
    for the hash's retained lifetime (F3). An absent job (``NoSuchJobError``) is
    not live either. On any *other* error fetching or reading status we return
    True (fail-safe): treat the chain as live so the caller does NOT re-seed on
    uncertainty and risk a duplicate chain — matching the prior uncertainty
    behaviour."""
    if not job_id:
        return False
    try:
        job = Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return False
    except Exception:
        return True
    try:
        return job.get_status(refresh=True) in _RUNNABLE_JOB_STATUSES
    except Exception:
        return True


def _generation_disposition(connection, occurrence_id: str):
    """Classify this occurrence against the chain-liveness marker (F1/F3).

    Returns ``(disposition, marker_value)`` where disposition is:
    - ``"canonical"`` — the marker names THIS occurrence: the one live generation.
    - ``"duplicate"`` — the marker names ANOTHER occurrence whose job is still
      LIVE (runnable): a true duplicate (replica race / manual enqueue) → exit,
      collapse.
    - ``"dead"`` — the marker is absent (chain expired past its TTL) OR names a
      job that no longer exists (phantom from a crash) OR names a job that has
      reached a TERMINAL state (its retained hash is not a live occurrence): the
      chain is lost and must be re-seeded in-band.
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
    # that occurrence's job is still LIVE (runnable); otherwise the marker is a
    # phantom (absent job) or names a terminal occurrence and the chain is dead.
    if _job_is_live(connection, marker):
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

    It also RELEASES the failed/killed occurrence's own singleton lock (an atomic
    compare-and-delete keyed to ``failed_id``): a hard SIGKILL/OOM leaves the
    lease held for up to ``LOCK_TTL_SECONDS``, which outlives the 300s interval
    floor, so without this the successor would find a stale lock and skip a sweep
    cycle. The compare-delete only clears the lock if the dead occurrence still
    owned it (a live sibling's lock is never touched — harmless no-op otherwise).
    This callback is invoked both by RQ's ``on_failure`` path (only attached to
    sweep occurrences) and by the worker's ``work_horse_killed_handler`` (which
    fires for ANY killed horse). It therefore **only acts for a sweep occurrence**
    (revision-8 F2): a killed non-sweep job (a paid analysis) must not trigger a
    re-seed — because the re-seed is enqueue-first, it would otherwise enqueue an
    orphan sweep occurrence before its CAS lost, churning Redis/the worker under
    repeated analysis failures. A non-sweep job also never owned the sweep lock,
    so there is nothing to release.
    """
    if not _is_sweep_occurrence(job):
        return
    try:
        failed_id = job.id if job is not None else ""
        # Free the dead occurrence's stale lease so the successor can sweep
        # immediately rather than waiting out LOCK_TTL_SECONDS.
        _release_lock(connection, failed_id)
        if _reseed_if_marker_is(connection, failed_id):
            logger.info("zep_sweep: on_failure re-seeded the occurrence chain")
    except Exception:
        logger.exception("zep_sweep: on_failure re-seed failed")


def on_work_horse_killed(job, retpid, ret_val, rusage) -> None:
    """RQ ``work_horse_killed_handler`` — runs in the SURVIVING parent worker
    process on a hard work-horse SIGKILL/OOM (which skips ``on_failure``).

    Delegates to :func:`reseed_chain_on_failure`, which (revision-8 F2) acts ONLY
    for a killed SWEEP occurrence — so a killed paid-analysis horse does not
    enqueue an orphan sweep occurrence. Module-level (not a worker closure) so the
    real killed-horse path has a committed regression test (revision-8 F3);
    ``worker.py`` wires it into the ``Worker`` constructor. It fetches its own
    Redis connection (the handler signature carries none) and guards so a hiccup
    can never crash the parent — the Sentry Cron monitor catches a still-dead
    chain independently."""
    if not _is_sweep_occurrence(job):
        return
    try:
        connection = get_redis_connection()
        if connection is not None:
            reseed_chain_on_failure(job, connection)
    except Exception:
        logger.warning(
            "zep_sweep: killed-horse re-seed failed; the Sentry Cron monitor will "
            "surface a dead chain"
        )


def _reseed_if_marker_is(connection, expected_id) -> bool:
    """Enqueue exactly one fresh occurrence and then atomically CAS-set the
    liveness marker to it, but ONLY while the marker is still ABSENT or equals
    ``expected_id``. Returns True iff this call won the marker.

    **Enqueue FIRST, then CAS the marker** (F4): the marker is only ever pointed
    at a job that has ALREADY been enqueued, so a hard process death between the
    two steps can never leave a phantom marker (one naming a job that was never
    scheduled) — the old claim-first ordering did, and the compare-delete
    rollback could not cover a death (it never ran). If the CAS LOSES (a racing
    re-seeder already won), the just-enqueued occurrence is a harmless ORPHAN: it
    is not marker-named, so when it runs it sees the marker naming the live winner
    and exits as a duplicate (generation ownership self-collapse). Racing
    re-seeders therefore enqueue N jobs, one wins the marker, and the other N-1
    each run once and exit — bounded, no loop.

    ``expected_id`` is ``""``/``None`` to CAS only against an absent marker, or the
    exact id the marker must still hold (a failed occurrence's own id, or a
    phantom / terminal id naming a job that is no longer live)."""
    queue = get_maintenance_queue(connection)
    if queue is None:
        return False
    occurrence_id = _new_occurrence_id()
    _enqueue_occurrence(queue, occurrence_id, sweep_interval_minutes())
    claimed = connection.eval(
        _RESEED_MARKER_LUA, 1, _NEXT_KEY,
        expected_id or "", occurrence_id, _marker_ttl_seconds(),
    )
    return bool(claimed)


def reconcile_chain(connection) -> None:
    """Boot reconciler (worker start): re-seed the chain unless the marker names
    a job that is still LIVE (F4). The r1 version trusted marker *presence* alone,
    so a crash between the marker claim and the enqueue left a phantom marker
    that no later boot would ever heal. Now: a healthy chain (marker present AND
    its job runnable) is left alone; an absent marker, a phantom one (names a
    non-existent job), OR one naming a TERMINAL occurrence (F3) is re-seeded via
    the atomic CAS (racing replicas seed once). Guarded so a Redis hiccup cannot
    crash worker boot."""
    try:
        marker = connection.get(_NEXT_KEY)
        if isinstance(marker, bytes):
            marker = marker.decode()
        if marker is not None and _job_is_live(connection, marker):
            return  # healthy chain — marker names a live scheduled/running job
        if _reseed_if_marker_is(connection, marker):
            logger.info("zep_sweep: boot reconciler seeded a fresh occurrence chain")
    except Exception:
        logger.warning("zep_sweep: boot reconciler failed; chain not seeded")
