"""Issue #72 (W2) - sweep scheduler integration tests (TDD §6 items 12-16).

These pin the revision-1/2/3 regressions and MUST run against **real Redis and
the pinned RQ version** - fakeredis cannot run an RQ Worker and lacks Lua
``eval`` (the lock release), so none of this can be faked. The suite is gated on
``WAKARU_TEST_REDIS_URL`` (skipped when unset so CI without Redis stays green);
run it against the throwaway instance the build brief starts on :6390:

    redis-cli -p 6390 ping >/dev/null 2>&1 || \
        redis-server --port 6390 --save '' --appendonly no --daemonize yes
    WAKARU_TEST_REDIS_URL=redis://127.0.0.1:6390/0 \
        OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES pytest -q tests/test_sweep_scheduling.py
"""
import os

import pytest

REDIS_URL = os.environ.get("WAKARU_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="WAKARU_TEST_REDIS_URL not set (real-Redis scheduler tests)"
)

import redis  # noqa: E402
from rq import SimpleWorker  # noqa: E402
from rq.job import Callback, Job  # noqa: E402

import app.services.maintenance_queue as mq  # noqa: E402
import app.services.zep_graph_sweeper as sweeper  # noqa: E402


@pytest.fixture
def rconn(monkeypatch):
    """A real Redis handle on a flushed dedicated DB, with ``REDIS_URL`` pointed
    at it so ``get_redis_connection()`` (and thus the queue helpers) resolve to
    the same instance. Flushes on entry and exit."""
    conn = redis.Redis.from_url(REDIS_URL)
    conn.flushdb()
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    # Deterministic, above the floor of 5.
    monkeypatch.setenv("ZEP_SWEEP_INTERVAL_MINUTES", "60")
    yield conn
    conn.flushdb()


def _enqueue_now(queue, occurrence_id):
    """Enqueue an occurrence to run immediately (for the burst worker), with the
    same options the scheduler uses."""
    return queue.enqueue(
        mq.run_sweep_occurrence,
        job_id=occurrence_id,
        job_timeout=mq.SWEEP_JOB_TIMEOUT,
        result_ttl=0,
        failure_ttl=86400,
        on_failure=Callback(mq.reseed_chain_on_failure),
    )


# --------------------------------------------------------------------------
# 12 - unique occurrence ids never touch a running job's hash (r1)
# --------------------------------------------------------------------------

def test_unique_occurrence_ids_never_touch_running_job(rconn):
    queue = mq.get_maintenance_queue(rconn)

    # Occurrence A is "running": its job hash carries a sentinel.
    occ_a = mq._new_occurrence_id()
    job_a = _enqueue_now(queue, occ_a)
    job_a.meta["sentinel"] = "A-running"
    job_a.save_meta()
    a_key = job_a.key
    a_created_at = rconn.hget(a_key, "created_at")

    # Schedule occurrence B through the real helper - a DIFFERENT unique id.
    occ_b = mq._new_occurrence_id()
    mq._enqueue_occurrence(queue, occ_b, 60)

    assert occ_a != occ_b
    # Scheduling B did not overwrite A's hash (the r1 failure: a reused id would).
    refetched = Job.fetch(occ_a, connection=rconn)
    assert refetched.meta.get("sentinel") == "A-running"
    assert rconn.hget(a_key, "created_at") == a_created_at

    # result_ttl=0 cleanup of A removes ONLY A's record; B survives.
    job_a.delete()
    assert not Job.exists(occ_a, connection=rconn)
    assert Job.exists(occ_b, connection=rconn)


# --------------------------------------------------------------------------
# 13 - on_failure re-seeds the chain (r2)
# --------------------------------------------------------------------------

def test_on_failure_reseeds_chain_direct(rconn):
    queue = mq.get_maintenance_queue(rconn)
    registry = queue.scheduled_job_registry
    assert registry.count == 0
    assert not rconn.exists(mq._NEXT_KEY)

    # Marker absent -> reseed claims it and enqueues exactly one occurrence.
    mq.reseed_chain_on_failure(None, rconn)
    assert registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None

    # Marker present -> SET NX no-ops; no second occurrence (idempotent).
    mq.reseed_chain_on_failure(None, rconn)
    assert registry.count == 1
    assert rconn.get(mq._NEXT_KEY) == marker


def test_on_failure_reseeds_chain_via_real_worker(rconn, monkeypatch):
    queue = mq.get_maintenance_queue(rconn)

    # Sweep body that STEALS the lock, then raises: A's own finally cannot
    # release (Lua compare mismatch) or reschedule, so the reschedule MUST come
    # from the on_failure callback - exercising that path for real.
    def steal_lock_and_raise(**kwargs):
        rconn.set(mq._LOCK_KEY, "some-other-owner")
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(mq, "sweep_orphan_graphs", steal_lock_and_raise)

    occ_a = mq._new_occurrence_id()
    _enqueue_now(queue, occ_a)

    worker = SimpleWorker([queue], connection=rconn)
    worker.work(burst=True)

    # The occurrence failed; on_failure re-seeded the chain exactly once.
    assert queue.scheduled_job_registry.count == 1
    assert rconn.exists(mq._NEXT_KEY)
    # A's finally did NOT release the stolen lock (compare-and-delete mismatch).
    assert rconn.get(mq._LOCK_KEY) == b"some-other-owner"
    # A itself ended failed, not finished.
    assert Job.fetch(occ_a, connection=rconn).is_failed


def test_on_failure_reseeds_when_marker_holds_dying_occurrence_id(rconn, monkeypatch):
    """r2 regression, with the REAL production precondition the sibling test
    above omits: while an occurrence runs, ``zep:sweep:next`` already holds
    *that occurrence's own id* (the prior occurrence set it when scheduling this
    one). If the sweep fails and its ``finally`` cannot advance the chain (lock
    lost/stolen, so ``_schedule_next`` never runs), the marker still equals the
    failing id. A bare ``SET NX`` re-seed would see the key present and decline,
    leaving the chain dead; the compare-and-set re-seed must claim it because it
    still equals the failed occurrence's id."""
    queue = mq.get_maintenance_queue(rconn)

    def steal_lock_and_raise(**kwargs):
        rconn.set(mq._LOCK_KEY, "some-other-owner")
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(mq, "sweep_orphan_graphs", steal_lock_and_raise)

    occ_a = mq._new_occurrence_id()
    # The marker holds occ_a's id: exactly the state its scheduler left behind,
    # and what production looks like while occ_a runs (contrast the marker-ABSENT
    # start of the sibling test, which masks the bug).
    rconn.set(mq._NEXT_KEY, occ_a, ex=mq._marker_ttl_seconds())
    _enqueue_now(queue, occ_a)

    worker = SimpleWorker([queue], connection=rconn)
    worker.work(burst=True)

    # occ_a failed and its finally could not advance the marker (stolen lock),
    # so on_failure saw the marker still == occ_a and re-seeded via CAS.
    assert Job.fetch(occ_a, connection=rconn).is_failed
    assert queue.scheduled_job_registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    # The chain advanced to a fresh occurrence id, not the dead occ_a.
    assert marker != occ_a.encode()
    assert marker.decode().startswith("zep-graph-sweep-")


# --------------------------------------------------------------------------
# 14 - boot reconciler seeds exactly one (SET NX)
# --------------------------------------------------------------------------

def test_boot_reconciler_seeds_exactly_one(rconn):
    queue = mq.get_maintenance_queue(rconn)
    registry = queue.scheduled_job_registry
    assert registry.count == 0
    assert not rconn.exists(mq._NEXT_KEY)

    mq.reconcile_chain(rconn)
    assert registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None

    # A racing replica (marker already claimed) seeds nothing more.
    mq.reconcile_chain(rconn)
    assert registry.count == 1
    assert rconn.get(mq._NEXT_KEY) == marker


def test_boot_reconciler_loser_does_not_double_seed(rconn):
    queue = mq.get_maintenance_queue(rconn)
    # Replica 1 wins the SET NX claim first (marker present, no scheduled job).
    interval = mq.sweep_interval_minutes()
    rconn.set(mq._NEXT_KEY, "winner-occurrence", nx=True, ex=3 * interval * 60)

    # Replica 2's reconcile must not enqueue anything - the claim is taken.
    mq.reconcile_chain(rconn)
    assert queue.scheduled_job_registry.count == 0
    assert rconn.get(mq._NEXT_KEY) == b"winner-occurrence"


# --------------------------------------------------------------------------
# 15 - duplicate chains collapse (loser exits without sweeping or rescheduling)
# --------------------------------------------------------------------------

def test_duplicate_chains_collapse(rconn, monkeypatch):
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )

    # Occurrence A holds the singleton lock (a live sweep in flight).
    rconn.set(mq._LOCK_KEY, "occurrence-A", nx=True, ex=mq.LOCK_TTL_SECONDS)
    before = queue.scheduled_job_registry.count

    # Occurrence B runs, finds the lock held, and must exit immediately.
    mq.run_sweep_occurrence()

    assert swept == []                                  # B never swept
    assert queue.scheduled_job_registry.count == before  # B never rescheduled
    assert rconn.get(mq._LOCK_KEY) == b"occurrence-A"    # A's lock untouched


# --------------------------------------------------------------------------
# 16 - lock release is atomic under lease expiry (r2 GET+DELETE race removed)
# --------------------------------------------------------------------------

def test_lock_release_atomic_under_lease_expiry(rconn):
    # A holds the lock; its lease expires; B acquires the fresh lock.
    rconn.set(mq._LOCK_KEY, "occurrence-A", nx=True, ex=mq.LOCK_TTL_SECONDS)
    rconn.delete(mq._LOCK_KEY)                       # simulate A's lease expiring
    rconn.set(mq._LOCK_KEY, "occurrence-B", nx=True, ex=mq.LOCK_TTL_SECONDS)

    # A's compare-and-delete release must NO-OP on B's lock...
    assert mq._release_lock(rconn, "occurrence-A") is False
    assert rconn.get(mq._LOCK_KEY) == b"occurrence-B"

    # ...while B's own release still works.
    assert mq._release_lock(rconn, "occurrence-B") is True
    assert rconn.get(mq._LOCK_KEY) is None


def test_lock_release_issues_single_atomic_eval(rconn):
    """The semantic test above is satisfied even by a NON-atomic
    GET-then-compare-then-DELETE, which is exactly the removed r2 race: the lease
    can expire between the GET and the DELETE, so the DELETE kills a successor's
    lock. Pin that release is a SINGLE server-side ``EVAL`` (atomic). A GET+DEL
    reimplementation calls ``get``/``delete`` and never ``eval``, failing here."""
    calls = []

    class RecordingConn:
        """Delegates to the real connection, recording every method name called
        so we can prove the release is one atomic EVAL, not GET+DELETE."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if callable(attr):
                def recorder(*args, **kwargs):
                    calls.append(name)
                    return attr(*args, **kwargs)

                return recorder
            return attr

    rconn.set(mq._LOCK_KEY, "occurrence-A", nx=True, ex=mq.LOCK_TTL_SECONDS)
    assert mq._release_lock(RecordingConn(rconn), "occurrence-A") is True

    assert "eval" in calls          # atomic compare-and-delete
    assert "get" not in calls       # not a GET+DELETE
    assert "delete" not in calls
    assert rconn.get(mq._LOCK_KEY) is None
