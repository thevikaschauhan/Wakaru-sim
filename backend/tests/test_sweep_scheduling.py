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
from rq.job import Callback, Job, JobStatus  # noqa: E402

import app.services.maintenance_queue as mq  # noqa: E402
import app.services.zep_graph_sweeper as sweeper  # noqa: E402


# Exact key namespaces this suite creates. Reset deletes ONLY these — NEVER a
# blanket FLUSHDB and NEVER a bare ``rq:*`` / ``rq:workers*`` wildcard — so a
# stray ``WAKARU_TEST_REDIS_URL`` pointed at a developer's Redis or a shared/prod
# endpoint can never destroy unrelated data (re-review F8). The occurrence job
# ids are ``zep-graph-sweep-<hex>``, so ``rq:job:zep-graph-sweep-*`` scopes our
# job hashes without touching another app's jobs on the same instance.
_SUITE_KEY_PATTERNS = (
    "zep:sweep:*", "zep:graph:*", "zep:scratch:*", "zep:merchant:*",
    "rq:job:zep-graph-sweep-*",
    "rq:queue:maintenance", "rq:queue:maintenance:*",
    "rq:scheduled:maintenance", "rq:scheduled_job_registry:maintenance",
    "rq:finished:maintenance", "rq:failed:maintenance",
    "rq:deferred:maintenance", "rq:canceled:maintenance",
    "rq:started:maintenance", "rq:wip:maintenance",
    "test:*",
)


def _reset_test_redis(conn, url) -> None:
    """Reset Redis between tests by deleting ONLY this suite's own namespaces, on
    ANY endpoint — never ``FLUSHDB``, never a bare ``rq:*`` wildcard (re-review
    F8). A stray ``WAKARU_TEST_REDIS_URL`` pointed at someone else's Redis must
    never wipe it; the worst case here is leaving a few of our own keys behind."""
    for pattern in _SUITE_KEY_PATTERNS:
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

    # Marker absent -> reseed enqueues one occurrence and CAS-sets the marker to it.
    mq.reseed_chain_on_failure(None, rconn)
    assert registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    assert marker.decode() in set(registry.get_job_ids())

    # A second on_failure re-seed does NOT advance or replace the canonical marker
    # (no second chain takes ownership). Under the enqueue-FIRST ordering (F4) the
    # CAS-losing reseeder enqueues a fresh occurrence, then its CAS no-ops because
    # the marker already names the winner; that leaves a harmless ORPHAN scheduled
    # job which self-collapses (as a duplicate) when it runs. The marker — the
    # single source of chain identity — stays pinned to the one canonical winner.
    mq.reseed_chain_on_failure(None, rconn)
    assert rconn.get(mq._NEXT_KEY) == marker              # canonical unchanged
    assert marker.decode() in set(registry.get_job_ids())  # winner still scheduled
    assert registry.count == 2                             # + one self-collapsing orphan


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
    # A healthy chain: a REAL scheduled occurrence exists and the marker names it
    # (F4 — reconcile trusts the marker only when its job actually exists).
    winner = mq._new_occurrence_id()
    mq._enqueue_occurrence(queue, winner, mq.sweep_interval_minutes())
    rconn.set(mq._NEXT_KEY, winner, nx=True, ex=mq._marker_ttl_seconds())
    before = queue.scheduled_job_registry.count      # 1 (winner scheduled)

    # Reconcile must not enqueue anything - the marker names a live job.
    mq.reconcile_chain(rconn)
    assert queue.scheduled_job_registry.count == before
    assert rconn.get(mq._NEXT_KEY) == winner.encode()


# --------------------------------------------------------------------------
# 15 - duplicate chains collapse via GENERATION OWNERSHIP (F1)
# --------------------------------------------------------------------------

def test_non_canonical_occurrence_declines_to_sweep(rconn, monkeypatch):
    """A duplicate occurrence — whose id is not the one the marker names, and the
    marker names a LIVE other occurrence — must exit at the generation gate: no
    sweep, no reschedule, lock never taken, even though the lock is FREE. This is
    the real serial case the old artificial-pre-held-lock test could not reach."""
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )

    # The marker names a DIFFERENT, LIVE occurrence (a real scheduled job); the
    # lock is free. Our occurrence is therefore a true duplicate, not a dead
    # chain — it exits without sweeping OR re-seeding.
    canonical = mq._new_occurrence_id()
    mq._enqueue_occurrence(queue, canonical, mq.sweep_interval_minutes())
    rconn.set(mq._NEXT_KEY, canonical, ex=mq._marker_ttl_seconds())
    before = queue.scheduled_job_registry.count          # 1 (canonical scheduled)

    # run_sweep_occurrence() outside a job context mints a fresh random id that
    # cannot equal the marker -> duplicate (marker names a live other) -> exits.
    mq.run_sweep_occurrence()

    assert swept == []                                   # never swept
    assert queue.scheduled_job_registry.count == before  # never rescheduled
    assert rconn.get(mq._LOCK_KEY) is None               # lock never taken
    assert rconn.get(mq._NEXT_KEY) == canonical.encode() # marker unchanged


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
    """F3/F4: the re-seed and boot-reconcile paths must never leave a live marker
    pointing at a job that was never enqueued. A phantom marker is fatal — the
    boot reconciler trusts a live marker and would never re-seed, so the chain
    stays permanently dead. Inject an enqueue failure and assert NO marker and NO
    scheduled job survive on either path. Under the enqueue-FIRST ordering (F4) an
    enqueue failure structurally cannot set the marker (the CAS runs only after a
    successful enqueue); the exact enqueue/marker ORDER is pinned separately by
    ``test_reseed_enqueues_before_setting_marker``."""
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
# 20 - F8: the reset fixture never FLUSHDBs and never touches foreign data
# --------------------------------------------------------------------------

class _ResetSpyConn:
    """Records which reset primitive the helper invoked, so we can prove it never
    FLUSHDBs any endpoint (re-review F8)."""

    def __init__(self):
        self.calls = []

    def flushdb(self):
        self.calls.append("flushdb")

    def scan_iter(self, match=None, count=None):
        self.calls.append(("scan_iter", match))
        return iter([])

    def delete(self, *keys):
        self.calls.append(("delete", keys))


def test_reset_never_flushdb_on_any_endpoint():
    """re-review F8: the reset helper must NEVER blanket-flush — not on a shared/
    prod endpoint AND not on loopback (a developer's local Redis is loopback too).
    It scope-deletes the suite's namespaces instead. Revert-proof: an
    unconditional ``flushdb()`` records ``flushdb`` here."""
    for url in (
        "redis://redis.internal.example.com:6379/0",   # shared/prod
        "redis://127.0.0.1:6390/0",                      # loopback (dev/test)
        "redis://localhost:6379/0",                      # loopback alias (CI)
    ):
        spy = _ResetSpyConn()
        _reset_test_redis(spy, url)
        assert "flushdb" not in spy.calls, f"reset flushed {url} (F8)"
        assert any(
            isinstance(c, tuple) and c[0] == "scan_iter" for c in spy.calls
        ), f"reset did not scope-delete for {url}"


def test_reset_leaves_foreign_keys_intact(rconn):
    """A key outside the suite's namespaces (another app's data) must SURVIVE a
    reset — the helper deletes only ``zep:*`` / maintenance-queue / our own job
    hashes / ``test:*`` (re-review F8)."""
    rconn.set("someone-elses:key", "keep-me")
    rconn.set("rq:job:some-other-app-job", "keep-me")     # a foreign RQ job
    rconn.zadd(sweeper._ACTIVE_KEY, {"mirofish_0123456789abcdef": 1})  # ours
    rconn.lpush("test:scratch", "ours")                    # ours

    _reset_test_redis(rconn, REDIS_URL)

    assert rconn.get("someone-elses:key") == b"keep-me"    # foreign survives
    assert rconn.get("rq:job:some-other-app-job") == b"keep-me"
    assert rconn.zscore(sweeper._ACTIVE_KEY, "mirofish_0123456789abcdef") is None  # ours gone
    assert not rconn.exists("test:scratch")                # ours gone


# --------------------------------------------------------------------------
# 21 - re-review F2: a lock-release failure must NOT end the chain
# --------------------------------------------------------------------------

def test_release_failure_still_schedules_successor(rconn, monkeypatch):
    """re-review F2: after a SUCCESSFUL sweep, a transient Redis error on lock
    release must NOT swallow the successor scheduling — the chain continues.
    Revert-proof: gating ``_schedule_next`` on ``_release_lock()`` returning True
    schedules zero successors when release fails."""
    queue = mq.get_maintenance_queue(rconn)
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())
    # Make lock release report failure (as a transient Redis error would).
    monkeypatch.setattr(mq, "_release_lock", lambda conn, occ: False)

    occ = mq._new_occurrence_id()
    rconn.set(mq._NEXT_KEY, occ, ex=mq._marker_ttl_seconds())   # occ is canonical
    _enqueue_now(queue, occ)

    worker = SimpleWorker([queue], connection=rconn)
    worker.work(burst=True)

    # The sweep ran and, despite the release "failure", scheduled exactly one
    # successor and advanced the marker.
    assert queue.scheduled_job_registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None and marker.decode() != occ


# --------------------------------------------------------------------------
# 22 - re-review F3: a dead / TTL-starved / phantom chain heals in-band
# --------------------------------------------------------------------------

def test_dead_chain_marker_absent_reseeds(rconn, monkeypatch):
    """re-review F3: an occurrence that dequeues after the marker's TTL has
    lapsed (marker ABSENT) must RE-SEED one fresh chain, not exit silently."""
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )
    assert not rconn.exists(mq._NEXT_KEY)      # TTL-starved: marker gone

    mq.run_sweep_occurrence()                  # fresh id, marker absent -> dead

    assert swept == []                         # this occurrence did not sweep
    assert queue.scheduled_job_registry.count == 1   # but re-seeded one chain
    assert rconn.exists(mq._NEXT_KEY)


def test_phantom_marker_occurrence_reseeds(rconn, monkeypatch):
    """re-review F3/F4: a marker naming a job that no longer exists (phantom from
    a crash) is a DEAD chain — an occurrence that sees it re-seeds in-band."""
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )
    # Marker names an id with NO scheduled/queued job -> phantom.
    rconn.set(mq._NEXT_KEY, "zep-graph-sweep-phantomdeadbeef", ex=mq._marker_ttl_seconds())

    mq.run_sweep_occurrence()

    assert swept == []
    assert queue.scheduled_job_registry.count == 1     # re-seeded
    marker = rconn.get(mq._NEXT_KEY).decode()
    assert marker != "zep-graph-sweep-phantomdeadbeef"  # advanced off the phantom
    assert marker in set(queue.scheduled_job_registry.get_job_ids())


def test_two_dead_chain_occurrences_reseed_exactly_one(rconn, monkeypatch):
    """Racing dead-chain heals collapse to ONE via the atomic claim — two
    occurrences finding the marker absent must not create two chains."""
    queue = mq.get_maintenance_queue(rconn)
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())
    assert not rconn.exists(mq._NEXT_KEY)

    mq.run_sweep_occurrence()
    marker_after_first = rconn.get(mq._NEXT_KEY)
    mq.run_sweep_occurrence()   # marker now names a live job -> duplicate -> exit

    assert queue.scheduled_job_registry.count == 1              # exactly one
    assert rconn.get(mq._NEXT_KEY) == marker_after_first        # unchanged


# --------------------------------------------------------------------------
# 23 - re-review F4: boot reconcile heals a phantom marker
# --------------------------------------------------------------------------

def test_reconcile_reseeds_phantom_marker(rconn):
    """re-review F4: the boot reconciler must re-seed when the marker names a job
    that does NOT exist (a crash between marker-claim and enqueue left a phantom).
    Revert-proof: trusting marker PRESENCE alone leaves the phantom forever."""
    queue = mq.get_maintenance_queue(rconn)
    rconn.set(mq._NEXT_KEY, "zep-graph-sweep-phantomdeadbeef", ex=mq._marker_ttl_seconds())
    assert queue.scheduled_job_registry.count == 0

    mq.reconcile_chain(rconn)

    assert queue.scheduled_job_registry.count == 1     # phantom healed
    marker = rconn.get(mq._NEXT_KEY).decode()
    assert marker != "zep-graph-sweep-phantomdeadbeef"
    assert marker in set(queue.scheduled_job_registry.get_job_ids())


# --------------------------------------------------------------------------
# 24 - re-review F3(a): the worker wires a killed-horse handler that re-seeds
# --------------------------------------------------------------------------

def test_worker_wires_killed_horse_handler_that_reseeds(rconn, monkeypatch):
    """re-review F3: RQ does NOT run on_failure on a hard work-horse SIGKILL, so
    the Worker must be constructed with a ``work_horse_killed_handler`` that
    re-seeds the chain from the surviving parent. Assert (a) worker.main wires the
    handler into the Worker, and (b) the handler actually re-seeds."""
    import worker as worker_module

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    captured = {}

    class _FakeWorker:
        def __init__(self, queues, connection=None, log_job_description=None,
                     work_horse_killed_handler=None):
            captured["handler"] = work_horse_killed_handler

        def work(self, with_scheduler=None):
            captured["worked"] = True

    monkeypatch.setattr(worker_module, "create_app", lambda: None)
    import rq as _rq
    monkeypatch.setattr(_rq, "Worker", _FakeWorker)

    worker_module.main()

    handler = captured["handler"]
    assert handler is not None, "Worker built without a work_horse_killed_handler (F3)"

    # The handler, given a (killed) job, re-seeds the chain in the parent process.
    _reset_test_redis(rconn, REDIS_URL)
    queue = mq.get_maintenance_queue(rconn)
    killed = mq._new_occurrence_id()
    rconn.set(mq._NEXT_KEY, killed, ex=mq._marker_ttl_seconds())   # marker == dead occ
    handler(_FakeKilledJob(killed), 1234, 9, None)

    assert queue.scheduled_job_registry.count == 1     # re-seeded from the handler
    assert rconn.get(mq._NEXT_KEY).decode() != killed


class _FakeKilledJob:
    def __init__(self, job_id):
        self.id = job_id


# --------------------------------------------------------------------------
# 25 - r3 (stale lock): a canonical occurrence that finds the lock held by a
#      dead prior occurrence still keeps the chain alive
# --------------------------------------------------------------------------

def test_canonical_lock_held_still_reschedules(rconn, monkeypatch):
    """r3 HIGH (stale lock): the min sweep interval (300s floor) is SHORTER than
    the lock TTL (360s), so a canonical successor can dequeue while a DEAD prior
    occurrence's lease still lingers. It must NOT exit empty (that leaves the chain
    permanently dead) — it schedules the successor + advances the marker WITHOUT
    sweeping this cycle, so the chain self-heals once the stale lease clears.
    Being canonical means the marker was already advanced to us, which only a
    prior occurrence's completion / post-death re-seed does; a still-running holder
    would keep the marker on itself. So the lock holder is provably dead and
    rescheduling is safe. Revert-proof: the old ``return`` leaves 0 successors and
    the marker stuck on the dead occurrence."""
    queue = mq.get_maintenance_queue(rconn)
    swept = []
    monkeypatch.setattr(
        mq, "sweep_orphan_graphs",
        lambda **kw: (swept.append(True), sweeper.SweepStats())[1],
    )

    # A stale lease from a dead prior occurrence still holds the lock.
    dead_occ = mq._new_occurrence_id()
    rconn.set(mq._LOCK_KEY, dead_occ, ex=mq.LOCK_TTL_SECONDS)

    # occ_a is the canonical successor (the marker names it) and dequeues now.
    occ_a = mq._new_occurrence_id()
    rconn.set(mq._NEXT_KEY, occ_a, ex=mq._marker_ttl_seconds())
    _enqueue_now(queue, occ_a)

    worker = SimpleWorker([queue], connection=rconn)
    worker.work(burst=True)

    assert swept == []                                    # lock held -> did not sweep
    assert queue.scheduled_job_registry.count == 1        # but scheduled the successor
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    successor = marker.decode()
    assert successor != occ_a                             # marker advanced off occ_a
    assert successor in set(queue.scheduled_job_registry.get_job_ids())
    # The stale lock is left untouched here (its TTL clears before the successor
    # runs); the killed-horse / on_failure path is what actively frees it.
    assert rconn.get(mq._LOCK_KEY) == dead_occ.encode()
    # occ_a itself finished cleanly (it did the safe reschedule, not a failure).
    assert occ_a not in queue.failed_job_registry.get_job_ids()


# --------------------------------------------------------------------------
# 26 - r3 (stale lock): the killed-horse / on_failure re-seed frees the dead
#      occurrence's own lock (compare-delete keyed to its id)
# --------------------------------------------------------------------------

def test_killed_horse_handler_releases_dead_lock(rconn):
    """r3 HIGH (stale lock): when an occurrence is hard-killed (or fails), the
    re-seed path must also RELEASE the dead occurrence's lock so the successor can
    sweep immediately instead of skipping a cycle behind a stale lease. The release
    is a compare-delete keyed to the dead id: it frees the lock only if that
    occurrence still owned it, and never touches a live sibling's lock. Revert-
    proof: without the release the dead occurrence's lock survives."""
    queue = mq.get_maintenance_queue(rconn)

    dead = mq._new_occurrence_id()
    rconn.set(mq._LOCK_KEY, dead, ex=mq.LOCK_TTL_SECONDS)          # dead occ holds the lock
    rconn.set(mq._NEXT_KEY, dead, ex=mq._marker_ttl_seconds())     # marker names the dead occ

    mq.reseed_chain_on_failure(_FakeKilledJob(dead), rconn)

    # The dead occurrence's own lease was compare-deleted, and exactly one fresh
    # chain re-seeded, advanced off the dead id.
    assert rconn.get(mq._LOCK_KEY) is None
    assert queue.scheduled_job_registry.count == 1
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None and marker.decode() != dead

    # Keying proof: the release is a compare-delete on the killed id, so it must
    # NOT touch a lock owned by a DIFFERENT (live) occurrence.
    live_owner = mq._new_occurrence_id()
    rconn.set(mq._LOCK_KEY, live_owner, ex=mq.LOCK_TTL_SECONDS)
    mq.reseed_chain_on_failure(_FakeKilledJob(dead), rconn)
    assert rconn.get(mq._LOCK_KEY) == live_owner.encode()          # live lock survives


# --------------------------------------------------------------------------
# 27 - r3 (_job_is_live): a marker naming a TERMINAL occurrence is a dead chain
# --------------------------------------------------------------------------

def test_terminal_job_marker_is_reseeded(rconn):
    """r3 HIGH (_job_is_live): RQ retains a FAILED / CANCELED job's hash
    (failure_ttl, the canceled registry), so ``Job.exists`` reads a dead
    occurrence as still present. A marker naming a TERMINAL occurrence must be
    treated as a DEAD chain and re-seeded, not mistaken for a live duplicate.
    Revert-proof: with the old hash-presence check the terminal hash reads as live
    and the chain is NOT re-seeded (stays dead for the hash's retained lifetime)."""
    queue = mq.get_maintenance_queue(rconn)
    terminal = mq._new_occurrence_id()
    job = _enqueue_now(queue, terminal)      # a real job hash
    job.set_status(JobStatus.FAILED)         # terminal, but the hash is retained

    # The retained hash still "exists" (why the old check misread it as live), but
    # the occurrence is NOT in a runnable state.
    assert Job.exists(terminal, connection=rconn)
    assert mq._job_is_live(rconn, terminal) is False

    rconn.set(mq._NEXT_KEY, terminal, ex=mq._marker_ttl_seconds())
    mq.reconcile_chain(rconn)

    # The terminal occurrence is NOT a live chain -> reconcile re-seeded exactly one
    # fresh occurrence and advanced the marker off the dead terminal id.
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    successor = marker.decode()
    assert successor != terminal
    assert successor in set(queue.scheduled_job_registry.get_job_ids())


# --------------------------------------------------------------------------
# 28 - r3 (F4): the re-seed enqueues the occurrence BEFORE it sets the marker
# --------------------------------------------------------------------------

def test_reseed_enqueues_before_setting_marker(rconn):
    """r3 F4: the re-seed must ENQUEUE the fresh occurrence BEFORE it CAS-sets the
    marker, so a hard process death between the two steps can never leave a phantom
    marker (one naming a job that was never enqueued). Assert that at the instant
    the marker-CAS EVAL runs, the job it will name ALREADY exists. Revert-proof:
    the old claim-FIRST ordering sets the marker before the enqueue, so the named
    job does not yet exist when the CAS runs."""
    observed = {}

    class OrderingConn:
        """Delegates to the real connection but records, at the moment the reseed
        marker-CAS EVAL runs, whether the occurrence it will name already exists
        and what the marker held (proving this CAS is the one that sets it)."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if name == "eval" and callable(attr):
                def wrapped(script, numkeys, *args):
                    if script == mq._RESEED_MARKER_LUA and args and args[0] == mq._NEXT_KEY:
                        occ = args[2]
                        occ = occ.decode() if isinstance(occ, bytes) else occ
                        observed["job_exists_at_cas"] = Job.exists(
                            occ, connection=self._inner
                        )
                        observed["marker_at_cas"] = self._inner.get(mq._NEXT_KEY)
                    return attr(script, numkeys, *args)

                return wrapped
            return attr

    assert not rconn.exists(mq._NEXT_KEY)
    seeded = mq._reseed_if_marker_is(OrderingConn(rconn), None)

    assert seeded is True
    assert observed.get("job_exists_at_cas") is True, (
        "marker CAS ran before the occurrence was enqueued (phantom-marker window)"
    )
    assert observed.get("marker_at_cas") is None   # this CAS is the one that set it
    marker = rconn.get(mq._NEXT_KEY)
    assert marker is not None
    scheduled = set(mq.get_maintenance_queue(rconn).scheduled_job_registry.get_job_ids())
    assert marker.decode() in scheduled
