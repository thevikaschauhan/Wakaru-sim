"""Issue #72 (W2) - orphan-graph sweeper unit tests (TDD §6 items 3-11).

Fake Zep client injected through ``zep_graph_sweeper._zep_client``; the Redis
ledger is fakeredis (the suite convention, see test_graph_lifecycle.py). These
cover the sweep algorithm in isolation - the lock/scheduler mechanics that need
a real RQ Worker + real Redis live in test_sweep_scheduling.py (fakeredis cannot
run a Worker and lacks Lua ``eval``, so they cannot be faked here).

Each test asserts the PROPERTY (which graphs were deleted, the memory bound, the
metric source), not merely that a method was called.
"""
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

import app.services.graph_lifecycle as gl
import app.services.maintenance_queue as mq
import app.services.zep_graph_sweeper as sweeper


# --------------------------------------------------------------------------
# Fixtures + fakes
# --------------------------------------------------------------------------

@pytest.fixture
def conn(monkeypatch):
    """A shared fakeredis wired into both the sweeper and the ledger it reads."""
    c = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(gl, "get_redis_connection", lambda: c)
    monkeypatch.setattr(sweeper, "get_redis_connection", lambda: c)
    return c


def scratch_id(n: int) -> str:
    """A structurally valid per-cart scratch id (``mirofish_`` + 16 hex)."""
    return f"mirofish_{n:016x}"


def iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class FakeGraph:
    def __init__(self, graph_id, created_at):
        self.graph_id = graph_id
        self.created_at = created_at


class FakeListResponse:
    def __init__(self, graphs, total_count=None):
        self.graphs = graphs
        self.total_count = total_count
        self.row_count = len(graphs) if graphs else 0


class FakeGraphApi:
    """Records the interleaving of list/delete calls so tests can assert both the
    no-delete-during-pagination rule and oldest-first deletion order."""

    def __init__(self, pages, fail_ids=None, total_count=None):
        self._pages = pages
        self.fail_ids = set(fail_ids or [])
        self.total_count = total_count
        self.events = []   # ("list", page_number) | ("delete", graph_id)
        self.deleted = []

    def list_all(self, page_number, page_size):
        self.events.append(("list", page_number))
        idx = page_number - 1
        graphs = self._pages[idx] if 0 <= idx < len(self._pages) else []
        return FakeListResponse(graphs, total_count=self.total_count)

    def delete(self, graph_id):
        self.events.append(("delete", graph_id))
        if graph_id in self.fail_ids:
            raise RuntimeError(f"delete boom for {graph_id}")
        self.deleted.append(graph_id)


class FakeZep:
    def __init__(self, graph_api):
        self.graph = graph_api


def install_client(monkeypatch, pages, fail_ids=None, total_count=None):
    api = FakeGraphApi(pages, fail_ids=fail_ids, total_count=total_count)
    monkeypatch.setattr(sweeper, "_zep_client", lambda: FakeZep(api))
    return api


@pytest.fixture
def captured_messages(monkeypatch):
    """Record sentry_sdk.capture_message calls (message, level)."""
    calls = []
    monkeypatch.setattr(
        sweeper.sentry_sdk, "capture_message",
        lambda msg, level=None: calls.append((msg, level)),
    )
    return calls


# --------------------------------------------------------------------------
# 3 - filters
# --------------------------------------------------------------------------

def test_sweeper_filters(conn, monkeypatch, captured_messages):
    old = scratch_id(1)                      # 48h -> deleted
    young = scratch_id(2)                     # 1h  -> kept
    merchant = "merchant_0123456789abcdef"    # store graph (#61) -> untouched
    shouting = "mirofish_ABCDEF0123456789"    # 16 chars, wrong case -> untouched
    pages = [[
        FakeGraph(old, iso_ago(48)),
        FakeGraph(young, iso_ago(1)),
        FakeGraph(merchant, iso_ago(48)),
        FakeGraph(shouting, iso_ago(48)),
    ]]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == [old]              # only the proven-old scratch graph
    assert stats.scanned == 4
    assert stats.matched == 2                # merchant_ and SHOUTING never match
    assert stats.eligible_total == 1
    assert stats.deleted == 1


# --------------------------------------------------------------------------
# 4 - fail-closed unknown age
# --------------------------------------------------------------------------

def test_sweeper_unknown_age_no_ledger_is_kept_counted_and_alerts(
    conn, monkeypatch, captured_messages
):
    gid = scratch_id(10)
    pages = [[FakeGraph(gid, "not-a-timestamp")]]   # unparseable vendor ts
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == []                          # never auto-deleted
    assert stats.skipped_unknown_age == 1
    assert stats.eligible_total == 0
    # would-alert: an unknown-age graph raises a Sentry message.
    assert any("skipped_unknown_age" in msg for msg, _ in captured_messages)


def test_sweeper_unknown_vendor_age_deleted_via_ledger_corroboration(
    conn, monkeypatch, captured_messages
):
    gid = scratch_id(11)
    # Ledger proves it older than the TTL (vendor timestamp is absent).
    conn.hset(f"zep:graph:{gid}", "created_at", str(int(time.time()) - 48 * 3600))
    pages = [[FakeGraph(gid, None)]]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert api.deleted == [gid]                       # corroboration path
    assert stats.skipped_unknown_age == 0
    assert stats.deleted == 1


# --------------------------------------------------------------------------
# 5 - dry-run parser matrix (only the `false` variants delete)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,should_delete", [
    (None, False), ("", False), ("true", False), ("TRUE", False),
    ("false", True), ("False", True), (" false ", True),
    ("0", False), ("no", False), ("garbage", False),
])
def test_sweeper_dry_run_parser(conn, monkeypatch, captured_messages, value, should_delete):
    if value is None:
        monkeypatch.delenv("ZEP_SWEEP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("ZEP_SWEEP_DRY_RUN", value)
    gid = scratch_id(20)
    api = install_client(monkeypatch, [[FakeGraph(gid, iso_ago(48))]])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=sweeper.sweep_dry_run(), ttl_hours=24, page_size=100, max_deletes=200
    )

    if should_delete:
        assert api.deleted == [gid]
        assert stats.skipped_dry_run == 0
    else:
        assert api.deleted == []
        assert stats.skipped_dry_run == 1


# --------------------------------------------------------------------------
# 6 - pagination + delete-after-list ordering
# --------------------------------------------------------------------------

def test_sweeper_pagination_deletes_after_full_listing_oldest_first(
    conn, monkeypatch, captured_messages
):
    g1, g2, g3 = scratch_id(31), scratch_id(32), scratch_id(33)
    pages = [
        [FakeGraph(g1, iso_ago(72)), FakeGraph(g2, iso_ago(48))],
        [FakeGraph(g3, iso_ago(36))],
    ]
    api = install_client(monkeypatch, pages)

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=2, max_deletes=200
    )

    list_positions = [i for i, e in enumerate(api.events) if e[0] == "list"]
    delete_positions = [i for i, e in enumerate(api.events) if e[0] == "delete"]
    # Every listing call precedes every delete - no deletion during pagination.
    assert max(list_positions) < min(delete_positions)
    # Oldest-first.
    assert api.deleted == [g1, g2, g3]
    assert stats.deleted == 3


# --------------------------------------------------------------------------
# 7 - partial failure
# --------------------------------------------------------------------------

def test_sweeper_partial_failure_continues_and_counts(
    conn, monkeypatch, captured_messages
):
    g1, g2, g3 = scratch_id(41), scratch_id(42), scratch_id(43)
    pages = [[
        FakeGraph(g1, iso_ago(72)),
        FakeGraph(g2, iso_ago(60)),
        FakeGraph(g3, iso_ago(48)),
    ]]
    api = install_client(monkeypatch, pages, fail_ids=[g2])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.failed == 1
    assert stats.deleted == 2
    assert g1 in api.deleted and g3 in api.deleted
    assert g2 not in api.deleted            # its delete raised, sweep continued


# --------------------------------------------------------------------------
# 8 - delete cap + bounded heap
# --------------------------------------------------------------------------

def test_sweeper_delete_cap_and_bounded_heap(conn, monkeypatch, captured_messages):
    heap_sizes = []
    monkeypatch.setattr(sweeper, "_heap_size_hook", lambda n: heap_sizes.append(n))

    ages = [90, 80, 70, 60, 50]             # all eligible (> 24h)
    by_age = {}
    page_one, page_two = [], []
    for i, hrs in enumerate(ages):
        gid = scratch_id(50 + i)
        by_age[hrs] = gid
        (page_one if i < 3 else page_two).append(FakeGraph(gid, iso_ago(hrs)))
    api = install_client(monkeypatch, [page_one, page_two])

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=3, max_deletes=2
    )

    assert stats.eligible_total == 5
    assert stats.deleted == 2
    assert stats.truncated_backlog == 3     # eligible_total - cap
    assert api.deleted == [by_age[90], by_age[80]]   # the two oldest, oldest first
    # Memory bound: the heap NEVER exceeded max_deletes across the multi-page scan.
    assert heap_sizes, "the bounded-heap hook must have fired"
    assert max(heap_sizes) <= 2


# --------------------------------------------------------------------------
# 9 - metrics from the scan, not Redis
# --------------------------------------------------------------------------

def test_metrics_oldest_age_from_scan_not_redis(conn, monkeypatch, captured_messages):
    gid = scratch_id(60)
    api = install_client(monkeypatch, [[FakeGraph(gid, iso_ago(100))]])
    # The registry claims it is brand new; the metric must ignore that and use
    # the 100h age from the Zep listing.
    conn.zadd(sweeper._ACTIVE_KEY, {gid: int(time.time())})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=True, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.oldest_scratch_age_seconds >= 99 * 3600


def test_metrics_registry_reconciliation_removes_stale(
    conn, monkeypatch, captured_messages
):
    live = scratch_id(70)                   # in the listing
    stale = scratch_id(71)                   # in the registry, gone from Zep
    api = install_client(monkeypatch, [[FakeGraph(live, iso_ago(1))]])
    now = int(time.time())
    conn.zadd(sweeper._ACTIVE_KEY, {live: now, stale: now})

    stats = sweeper.sweep_orphan_graphs(
        dry_run=False, ttl_hours=24, page_size=100, max_deletes=200
    )

    assert stats.ledger_drift == 1
    assert conn.zscore(sweeper._ACTIVE_KEY, stale) is None   # reconciled away
    assert conn.zscore(sweeper._ACTIVE_KEY, live) is not None  # still live


# --------------------------------------------------------------------------
# 10 - ttl floor guard
# --------------------------------------------------------------------------

def test_ttl_floor(monkeypatch):
    monkeypatch.delenv("ZEP_GRAPH_TTL_HOURS", raising=False)
    assert sweeper.sweep_ttl_hours() == 24            # default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "3")
    assert sweeper.sweep_ttl_hours() == 24            # below floor -> default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "5")
    assert sweeper.sweep_ttl_hours() == 24            # below floor (6) -> default
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "6")
    assert sweeper.sweep_ttl_hours() == 6             # exact floor honoured
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "48")
    assert sweeper.sweep_ttl_hours() == 48            # above floor honoured
    monkeypatch.setenv("ZEP_GRAPH_TTL_HOURS", "abc")
    assert sweeper.sweep_ttl_hours() == 24            # non-integer -> default


# --------------------------------------------------------------------------
# 11 - cron check-in emitted (the occurrence body wraps the sweep)
# --------------------------------------------------------------------------

class CheckinRecorder:
    def __init__(self):
        self.calls = []   # (status, check_in_id)

    def __call__(self, *, monitor_slug, check_in_id=None, status=None,
                 duration=None, monitor_config=None):
        self.calls.append((status, check_in_id))
        return check_in_id or "test-check-in-id"


def test_cron_checkin_in_progress_then_ok_on_success(monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(mq, "capture_checkin", rec)
    monkeypatch.setattr(mq, "sweep_orphan_graphs", lambda **kw: sweeper.SweepStats())

    mq._run_sweep_with_checkin()

    assert [s for s, _ in rec.calls] == [mq.MonitorStatus.IN_PROGRESS, mq.MonitorStatus.OK]
    # The OK check-in reuses the id returned by the IN_PROGRESS check-in.
    assert rec.calls[1][1] == "test-check-in-id"


def test_cron_checkin_in_progress_then_error_on_raise(monkeypatch):
    rec = CheckinRecorder()
    monkeypatch.setattr(mq, "capture_checkin", rec)

    def boom(**kw):
        raise RuntimeError("sweep boom")

    monkeypatch.setattr(mq, "sweep_orphan_graphs", boom)

    with pytest.raises(RuntimeError):
        mq._run_sweep_with_checkin()

    assert [s for s, _ in rec.calls] == [mq.MonitorStatus.IN_PROGRESS, mq.MonitorStatus.ERROR]
    assert rec.calls[1][1] == "test-check-in-id"
