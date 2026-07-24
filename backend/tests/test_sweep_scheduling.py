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
from redis.exceptions import RedisError  # noqa: E402
from rq import SimpleWorker, Worker  # noqa: E402
from rq.job import Callback, Job  # noqa: E402

import app.services.maintenance_queue as mq  # noqa: E402
import app.services.zep_graph_sweeper as sweeper  # noqa: E402


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def _redis_host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def _reset_test_redis(conn, url) -> None:
    """Reset Redis between tests. ``FLUSHDB`` only when the endpoint is loopback;
    on any non-loopback (shared/prod) endpoint, REFUSE the blanket flush and
    delete only the namespaces this suite touches (F10 — a stray
    ``WAKARU_TEST_REDIS_URL`` pointed at someone else's Redis must never wipe
    it)."""
    if _redis_host(url) in _LOOPBACK_HOSTS:
        conn.flushdb()
        return
    for pattern in (
        "zep:sweep:*", "zep:graph:*", "zep:scratch:*", "zep:merchant:*",
        "rq:*", "test:*",
    ):
        keys = list(conn.scan_iter(match=pattern, count=500))
        if keys:
            conn.delete(*keys)


@pytest.fixture
def rconn(monkeypatch):
    """A real Redis handle on a reset dedicated DB, with ``REDIS_URL`` pointed at
    it so ``get_redis_connection()`` (and thus the queue helpers) resolve to the
    same instance. Reset on entry and exit (loopback-guarded — see
    ``_reset_test_redis``)."""
    conn = redis.Redis.from_url(REDIS_URL)
    _reset_test_redis(conn, REDIS_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    # Deterministic, above the floor of 5.
    monkeypatch.setenv("ZEP_SWEEP_INTERVAL_MINUTES", "60")
    yield conn
    _reset_test_redis(conn, REDIS_URL)


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
    # The marker names occ_a: it is the canonical generation (generation
    # ownership — occ_a would otherwise decline to sweep). This is the state its
    # scheduler leaves behind while it runs.
    rconn.set(mq._NEXT_KEY, occ_a, ex=mq._marker_ttl_seconds())
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
# 15 - duplicate chains collapse via GENERATION OWNERSHIP (F1)
# --------------------------------------------------------------------------

def test_non_canonical_occurrence_declines_to_sweep(rconn, monkeypatch):
    """An occurrence whose id is not the one the marker names must exit at the
    generation-ownership gate — no sweep, no reschedule, lock never taken — even
    though the lock is FREE. This is the real serial case the old
    artificial-pre-held-lock test could not reach."""
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )

    # The marker names a DIFFERENT occurrence; the lock is free.
    rconn.set(
        mq._NEXT_KEY, "some-other-canonical-occurrence", ex=mq._marker_ttl_seconds()
    )
    before = queue.scheduled_job_registry.count

    # run_sweep_occurrence() outside a job context mints a fresh random id that
    # cannot equal the marker -> non-canonical -> declines before the lock.
    mq.run_sweep_occurrence()

    assert swept == []                                   # never swept
    assert queue.scheduled_job_registry.count == before  # never rescheduled
    assert rconn.get(mq._LOCK_KEY) is None               # lock never taken


def test_serialized_duplicate_occurrences_collapse_to_one(rconn, monkeypatch):
    """F1 regression, reproduced against a real FORKING RQ ``Worker`` in burst.

    On one worker, queued occurrences run SERIALLY: the singleton lock alone would
    let A acquire the free lock, sweep, release, reschedule A', then B acquire the
    now-free lock, sweep, reschedule B' -> two permanent chains. The old
    ``test_duplicate_chains_collapse`` masked this by artificially pre-holding the
    lock so the loser never ran its own chain. Generation ownership fixes it: the
    marker names ONE canonical occurrence; the other exits without sweeping or
    rescheduling. Assert exactly one sweep ran and exactly one successor was
    scheduled. Revert-proof: with the generation gate removed, both occurrences
    sweep and reschedule (sweep_count == 2, registry.count == 2)."""
    queue = mq.get_maintenance_queue(rconn)

    # Count sweeps in Redis: a forked child's in-memory state is invisible to this
    # (parent) process, so an in-memory list would always read empty.
    def counting_sweep(**kwargs):
        import redis as _redis

        _redis.Redis.from_url(REDIS_URL).incr("test:sweep_count")
        return sweeper.SweepStats()

    monkeypatch.setattr(mq, "sweep_orphan_graphs", counting_sweep)

    occ_a = mq._new_occurrence_id()
    occ_b = mq._new_occurrence_id()
    # The marker names ONLY occ_a -> occ_a is canonical, occ_b is the duplicate (a
    # manual re-enqueue / replica race that landed a second occurrence).
    rconn.set(mq._NEXT_KEY, occ_a, ex=mq._marker_ttl_seconds())
    _enqueue_now(queue, occ_a)
    _enqueue_now(queue, occ_b)

    # A real forking Worker in burst == the production execution model.
    worker = Worker([queue], connection=rconn)
    worker.work(burst=True)

    # Exactly ONE sweep ran (canonical occ_a); occ_b exited at the gate.
    assert rconn.get("test:sweep_count") == b"1"
    # Exactly ONE successor scheduled -> the chain stayed single, not doubled.
    assert queue.scheduled_job_registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    successor_id = marker.decode()
    # The chain advanced past occ_a to a single fresh successor, distinct from
    # both enqueued occurrences.
    assert successor_id.startswith("zep-graph-sweep-")
    assert successor_id not in (occ_a, occ_b)
    assert set(queue.scheduled_job_registry.get_job_ids()) == {successor_id}


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


# --------------------------------------------------------------------------
# 17 - F3: no phantom marker on enqueue failure (re-seed AND boot paths)
# --------------------------------------------------------------------------

def test_no_phantom_marker_when_enqueue_fails(rconn, monkeypatch):
    """F3: the re-seed and boot-reconcile paths must never leave a live marker
    pointing at a job that was never enqueued. A phantom marker is fatal — the
    boot reconciler trusts marker presence and would never re-seed, so the chain
    stays permanently dead. Inject an enqueue failure and assert NO marker and NO
    scheduled job survive on either path. Revert-proof: the marker-before-enqueue
    ordering leaves the claimed marker behind (``exists`` is True)."""
    queue = mq.get_maintenance_queue(rconn)

    def boom(*args, **kwargs):
        raise RedisError("enqueue exploded")

    monkeypatch.setattr(mq, "_enqueue_occurrence", boom)

    # on_failure re-seed path: marker absent -> CAS claims it -> enqueue fails ->
    # the claim must be rolled back (compare-delete the exact id).
    assert not rconn.exists(mq._NEXT_KEY)
    mq.reseed_chain_on_failure(None, rconn)
    assert not rconn.exists(mq._NEXT_KEY), "reseed left a phantom marker"
    assert queue.scheduled_job_registry.count == 0

    # boot reconciler path: SET NX claims -> enqueue fails -> claim rolled back.
    mq.reconcile_chain(rconn)
    assert not rconn.exists(mq._NEXT_KEY), "boot reconcile left a phantom marker"
    assert queue.scheduled_job_registry.count == 0


# --------------------------------------------------------------------------
# 18 - F3: a failed successor schedule FAILS the occurrence (not swallowed)
# --------------------------------------------------------------------------

def test_schedule_failure_fails_the_occurrence(rconn, monkeypatch):
    """F3: if scheduling the successor fails, the occurrence must FAIL (raise) so
    ``on_failure``/monitoring heals the chain. A swallowed schedule error lets the
    occurrence SUCCEED while the chain silently dies. The sweep here succeeds but
    the successor enqueue raises; assert the occurrence lands in the failed
    registry. Revert-proof: a reverted swallow finishes the occurrence and
    ``result_ttl=0`` deletes it, so it is absent from the failed registry."""
    queue = mq.get_maintenance_queue(rconn)
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())

    def boom(*args, **kwargs):
        raise RedisError("enqueue exploded")

    occ_a = mq._new_occurrence_id()
    rconn.set(mq._NEXT_KEY, occ_a, ex=mq._marker_ttl_seconds())  # occ_a canonical
    _enqueue_now(queue, occ_a)                                   # real enqueue first
    monkeypatch.setattr(mq, "_enqueue_occurrence", boom)         # then break scheduling

    worker = SimpleWorker([queue], connection=rconn)
    worker.work(burst=True)

    assert occ_a in queue.failed_job_registry.get_job_ids()


# --------------------------------------------------------------------------
# 19 - F4: the Sentry cron check-in upserts a monitor_config
# --------------------------------------------------------------------------

def test_cron_checkin_passes_monitor_config(monkeypatch):
    """F4: without a ``monitor_config`` Sentry has no schedule to compare check-ins
    against and cannot detect a MISSED occurrence — the external-watcher guarantee
    is non-functional. Assert every check-in carries a monitor_config whose
    interval schedule is derived from ``ZEP_SWEEP_INTERVAL_MINUTES``. Revert-proof:
    dropping monitor_config makes ``cfg`` None."""
    monkeypatch.setenv("ZEP_SWEEP_INTERVAL_MINUTES", "60")
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())

    calls = []

    def fake_checkin(**kwargs):
        calls.append(kwargs)
        return "checkin-id"

    monkeypatch.setattr(mq, "capture_checkin", fake_checkin)

    mq._run_sweep_with_checkin()

    assert calls, "no check-in captured"
    for call in calls:
        cfg = call.get("monitor_config")
        assert cfg is not None, "check-in missing monitor_config (F4)"
        assert cfg["schedule"] == {"type": "interval", "value": 60, "unit": "minute"}
        assert cfg["checkin_margin"] >= 1
        assert cfg["max_runtime"] >= 1
    assert len(calls) == 2   # in_progress + ok on the success path


# --------------------------------------------------------------------------
# 20 - F10: the reset fixture never blanket-flushes a non-loopback endpoint
# --------------------------------------------------------------------------

class _ResetSpyConn:
    """Records which reset primitive the helper invoked, so we can prove it never
    FLUSHDBs a shared/prod endpoint."""

    def __init__(self):
        self.calls = []

    def flushdb(self):
        self.calls.append("flushdb")

    def scan_iter(self, match=None, count=None):
        self.calls.append(("scan_iter", match))
        return iter([])

    def delete(self, *keys):
        self.calls.append(("delete", keys))


def test_reset_refuses_flushdb_on_non_loopback_endpoint():
    """F10: a stray ``WAKARU_TEST_REDIS_URL`` pointing at someone else's (shared
    or prod) Redis must NEVER be blanket-flushed. Assert the reset helper does
    NOT call ``flushdb`` for a non-loopback host and scoped-deletes instead.
    Revert-proof: an unconditional ``flushdb()`` records ``flushdb`` here."""
    spy = _ResetSpyConn()
    _reset_test_redis(spy, "redis://redis.internal.example.com:6379/0")
    assert "flushdb" not in spy.calls, "reset flushed a non-loopback endpoint (F10)"
    assert any(
        isinstance(c, tuple) and c[0] == "scan_iter" for c in spy.calls
    ), "reset did not fall back to scoped key deletion"


def test_reset_flushes_only_loopback():
    """The guard must not over-refuse: a loopback endpoint (the throwaway test
    instance) is still fully flushed."""
    spy = _ResetSpyConn()
    _reset_test_redis(spy, "redis://127.0.0.1:6390/0")
    assert spy.calls == ["flushdb"]
