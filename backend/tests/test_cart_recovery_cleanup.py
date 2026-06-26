"""Issue #24 (CP2b) — per-analysis scratch cleanup.

run_cart_recovery removes the project / simulation / report dirs it writes under
uploads/ once the insight is produced (success or failure), so PII-bearing
scratch with no live reader does not co-mingle across tenants or accumulate.
These tests cover the manager delete primitives and the _cleanup_artifacts
orchestrator in isolation; the finally-wiring (cleanup fires on success AND
failure, with the right ids) is exercised in test_cart_recovery_workflow.py.
"""
from types import SimpleNamespace

import app.services.cart_recovery_workflow as wf
from app.services.report_agent import ReportManager
from app.services.simulation_manager import SimulationManager


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
