"""Issue #24 (CP2b) + issue #72 - per-analysis scratch cleanup.

run_cart_recovery removes the project / simulation / report dirs it writes under
uploads/ once the insight is produced (success or failure), so PII-bearing
scratch with no live reader does not co-mingle across tenants or accumulate.
Issue #72 adds a fourth guarded cleanup block: the run's throwaway Zep graph is
deleted inline and its lifecycle-ledger record closed. These tests cover the
manager delete primitives and the _cleanup_artifacts orchestrator in isolation;
the finally-wiring (cleanup fires on success AND failure, with the right ids)
is exercised in test_cart_recovery_workflow.py.
"""
from types import SimpleNamespace

import app.services.cart_recovery_workflow as wf
from app.services.report_agent import ReportManager
from app.services.simulation_manager import SimulationManager

FAKE_GRAPH_ID = "mirofish_0123456789abcdef"


# --- manager delete primitives -----------------------------------------------

def test_delete_simulation_removes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path / "simulations"))
    sim_dir = tmp_path / "simulations" / "sim_0123456789ab"
    sim_dir.mkdir(parents=True)
    (sim_dir / "state.json").write_text("{}")

    assert SimulationManager.delete_simulation("sim_0123456789ab") is True
    assert not sim_dir.exists()
    # Absent dir → False (idempotent; safe to call on an already-cleaned run).
    assert SimulationManager.delete_simulation("sim_0123456789ab") is False


def test_delete_report_removes_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(tmp_path / "reports"))
    folder = tmp_path / "reports" / "report_0123456789ab"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text("{}")

    assert ReportManager.delete_report("report_0123456789ab") is True
    assert not folder.exists()
    assert ReportManager.delete_report("report_0123456789ab") is False


# --- _cleanup_artifacts orchestration ----------------------------------------

def test_cleanup_artifacts_deletes_project_sim_and_report(monkeypatch):
    calls = []
    monkeypatch.setattr(
        wf.ReportManager, "get_report_by_simulation",
        lambda sid: SimpleNamespace(report_id="report_x"),
    )
    monkeypatch.setattr(wf.ReportManager, "delete_report", lambda rid: calls.append(("report", rid)))
    monkeypatch.setattr(wf.ProjectManager, "delete_project", lambda pid: calls.append(("project", pid)))
    monkeypatch.setattr(wf.SimulationManager, "delete_simulation", lambda sid: calls.append(("sim", sid)))

    wf._cleanup_artifacts("proj_x", "sim_x")

    assert ("project", "proj_x") in calls
    assert ("sim", "sim_x") in calls
    assert ("report", "report_x") in calls


def test_cleanup_artifacts_without_simulation_deletes_project_only(monkeypatch):
    # An early failure (before the simulation exists) passes simulation_id=None;
    # only the project is cleaned, and the report lookup is skipped.
    calls = []

    def _should_not_be_called(sid):
        raise AssertionError("get_report_by_simulation must not run without a simulation_id")

    monkeypatch.setattr(wf.ReportManager, "get_report_by_simulation", _should_not_be_called)
    monkeypatch.setattr(wf.ProjectManager, "delete_project", lambda pid: calls.append(("project", pid)))

    wf._cleanup_artifacts("proj_x", None)

    assert calls == [("project", "proj_x")]


def test_cleanup_artifacts_is_best_effort(monkeypatch):
    # Every removal is independently guarded: a failure in any one must not block
    # the others or propagate — cleanup runs in run_cart_recovery's finally and
    # must never mask the analysis result. All three raise here, so the only way
    # all three are attempted is if each has its own guard.
    attempted = []

    def _boom(name):
        def _raise(_arg):
            attempted.append(name)
            raise OSError(f"{name} delete failed")
        return _raise

    monkeypatch.setattr(
        wf.ReportManager, "get_report_by_simulation",
        lambda sid: SimpleNamespace(report_id="report_x"),
    )
    monkeypatch.setattr(wf.ProjectManager, "delete_project", _boom("project"))
    monkeypatch.setattr(wf.SimulationManager, "delete_simulation", _boom("sim"))
    monkeypatch.setattr(wf.ReportManager, "delete_report", _boom("report"))

    wf._cleanup_artifacts("proj_x", "sim_x")  # must not raise despite all three failing

    assert attempted == ["project", "sim", "report"]


# --- #72: fourth guarded block - inline Zep graph delete ----------------------

def _stub_local_deletes(monkeypatch):
    monkeypatch.setattr(wf.ReportManager, "get_report_by_simulation", lambda sid: None)
    monkeypatch.setattr(wf.ProjectManager, "delete_project", lambda pid: None)
    monkeypatch.setattr(wf.SimulationManager, "delete_simulation", lambda sid: None)


def test_cleanup_artifacts_deletes_zep_graph_and_closes_ledger(monkeypatch):
    _stub_local_deletes(monkeypatch)
    deleted_graphs, ledger = [], []

    class FakeBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            deleted_graphs.append(graph_id)

    monkeypatch.setattr(wf, "GraphBuilderService", FakeBuilder)
    monkeypatch.setattr(
        wf.graph_lifecycle, "record_deleted", lambda gid, source: ledger.append((gid, source))
    )

    wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")

    assert deleted_graphs == [FAKE_GRAPH_ID]
    assert ledger == [(FAKE_GRAPH_ID, "inline")]


def test_cleanup_artifacts_zep_delete_failure_is_guarded(monkeypatch):
    # A real Zep delete failure must neither propagate (the block runs in the
    # analysis finally) nor close the ledger record of a still-existing graph
    # (the W2 sweeper needs to find it).
    _stub_local_deletes(monkeypatch)
    ledger = []

    class BoomBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            raise OSError("zep unreachable")

    monkeypatch.setattr(wf, "GraphBuilderService", BoomBuilder)
    monkeypatch.setattr(
        wf.graph_lifecycle, "record_deleted", lambda gid, source: ledger.append((gid, source))
    )

    wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")  # must not raise

    assert ledger == []


def test_cleanup_artifacts_non_404_api_error_keeps_ledger_live(monkeypatch):
    # A non-404 Zep ApiError is a real failure: the graph may still exist, so
    # the ledger record stays live for the W2 sweeper to retry.
    _stub_local_deletes(monkeypatch)
    ledger = []

    class ServerErrorBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            raise wf.ApiError(status_code=500, body="boom")

    monkeypatch.setattr(wf, "GraphBuilderService", ServerErrorBuilder)
    monkeypatch.setattr(
        wf.graph_lifecycle, "record_deleted", lambda gid, source: ledger.append((gid, source))
    )

    wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")  # must not raise

    assert ledger == []


def test_cleanup_artifacts_ledger_close_failure_is_guarded(monkeypatch):
    # record_deleted runs in run_cart_recovery's finally, so it must NOT raise
    # even on a non-Redis failure (e.g. a decode error on corrupt ledger bytes);
    # an escaping exception would mask the analysis result or a pipeline
    # exception. The delete itself succeeds here, so the ledger close is reached.
    _stub_local_deletes(monkeypatch)

    class FakeBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            pass

    def boom(gid, source):
        raise ValueError("invalid start byte")  # a non-RedisError, as corrupt UTF-8 would raise

    monkeypatch.setattr(wf, "GraphBuilderService", FakeBuilder)
    monkeypatch.setattr(wf.graph_lifecycle, "record_deleted", boom)

    # Must not raise.
    wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")


def test_cleanup_artifacts_non_404_error_never_logs_response_body(monkeypatch):
    # A non-404 Zep ApiError must log the status code and type only, never the
    # ApiError's str() (which includes headers/body that can echo response
    # content). Reproduces an email marker in the error body. Captures via a
    # handler on the logger directly (the mirofish logger sets propagate=False,
    # so caplog only works under the Flask app fixture, which this unit test
    # does not use).
    import logging

    _stub_local_deletes(monkeypatch)
    pii = "pii-marker@example.com"

    class BoomBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            raise wf.ApiError(status_code=500, body=f"upstream error for {pii}")

    monkeypatch.setattr(wf, "GraphBuilderService", BoomBuilder)
    monkeypatch.setattr(wf.graph_lifecycle, "record_deleted", lambda gid, source: None)

    messages = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger = logging.getLogger("mirofish.cart_recovery")
    logger.addHandler(handler)
    try:
        wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")
    finally:
        logger.removeHandler(handler)

    joined = "\n".join(messages)
    assert pii not in joined, "response body (with PII) leaked into the log"
    assert "status=500" in joined and "ApiError" in joined


def test_cleanup_artifacts_already_deleted_404_closes_ledger(monkeypatch):
    # An expected 404 (a concurrent cleanup, or a future offboard path, already
    # removed the graph) is a successful end state, not a failure. The ledger
    # must be closed so the id leaves the active/merchant sets and does not
    # linger without a TTL (issue-72 TDD V-1).
    _stub_local_deletes(monkeypatch)
    ledger = []

    class GoneBuilder:
        def __init__(self, api_key=None):
            pass

        def delete_graph(self, graph_id):
            raise wf.ApiError(status_code=404, body="graph not found")

    monkeypatch.setattr(wf, "GraphBuilderService", GoneBuilder)
    monkeypatch.setattr(
        wf.graph_lifecycle, "record_deleted", lambda gid, source: ledger.append((gid, source))
    )

    wf._cleanup_artifacts("proj_x", "sim_x", FAKE_GRAPH_ID, "merchant-uuid")

    assert ledger == [(FAKE_GRAPH_ID, "inline")]


def test_cleanup_artifacts_without_graph_skips_zep_delete(monkeypatch):
    # graph_id=None (failure before graph creation, or a legacy 2-arg call):
    # the Zep client must not even be constructed.
    _stub_local_deletes(monkeypatch)

    class MustNotConstruct:
        def __init__(self, api_key=None):
            raise AssertionError("GraphBuilderService must not be built without a graph_id")

    monkeypatch.setattr(wf, "GraphBuilderService", MustNotConstruct)

    wf._cleanup_artifacts("proj_x", "sim_x", None, "merchant-uuid")
    wf._cleanup_artifacts("proj_x", "sim_x")  # defaulted legacy signature
