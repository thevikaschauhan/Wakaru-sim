"""Tests for the OASIS database path wiring (issue #45).

OASIS's ``get_db_path()`` defaults to a ``data/`` dir inside its own
site-packages install and ``os.makedirs()`` it. Under the non-root ``appuser``
container (the #9 / PR #36 hardening) that path is read-only, so every full
pipeline run died with ``PermissionError`` at the OASIS simulation step.

``start_simulation`` now points OASIS at the per-simulation directory via the
``OASIS_DB_PATH`` env var on the launched subprocess. ``get_db_path()`` returns
that value verbatim (no ``makedirs``), so it must be an absolute path under an
already-existing, writable dir — the subprocess runs with ``cwd=sim_dir``, so a
relative value would resolve against cwd and double-nest. The filename
``social_media.db`` is OASIS's own ``DB_NAME`` default — the file its no-arg
``get_db_path()`` callers (agent_environment.py:71/89) read; it is intentionally
distinct from the main ``twitter_simulation.db`` passed to ``oasis.make()``.
"""
import json
import os
from unittest.mock import MagicMock

import pytest

from app.services import simulation_runner as sr
from app.services.simulation_runner import SimulationRunner

# Process-global caches start_simulation() populates; pop per-sim so a stubbed
# run doesn't leak into a sibling test or the atexit cleanup_all_simulations().
# _graph_memory_enabled gets a {sim_id: False} entry even with graph memory off —
# a non-empty dict keeps cleanup's has_updaters truthy and logs into pytest's
# closed stream, so it must be popped too.
_SIM_CACHES = (
    "_run_states",
    "_processes",
    "_monitor_threads",
    "_stdout_files",
    "_stderr_files",
    "_graph_memory_enabled",
    "_action_queues",
)


def _drop_sim_caches(sim_id):
    for name in _SIM_CACHES:
        getattr(SimulationRunner, name).pop(sim_id, None)


def _install_launch_stubs(monkeypatch):
    """Stub the subprocess launch + monitor thread; return the dict capturing
    the Popen call's cmd/cwd/env."""
    captured = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(sr.subprocess, "Popen", fake_popen)
    # Don't start the real monitor thread (it would tail logs of a dead proc).
    monkeypatch.setattr(sr.threading, "Thread", lambda *a, **k: MagicMock())
    return captured


def _write_config(sim_dir):
    (sim_dir / "simulation_config.json").write_text(
        json.dumps({"time_config": {"total_simulation_hours": 1, "minutes_per_round": 30}}),
        encoding="utf-8",
    )


@pytest.fixture
def prepared_sim(tmp_path, monkeypatch):
    """A minimal prepared simulation under an absolute RUN_STATE_DIR."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    SimulationRunner._run_states.clear()
    sim_id = "sim_test_45"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir()
    _write_config(sim_dir)
    captured = _install_launch_stubs(monkeypatch)

    yield sim_id, sim_dir, captured

    _drop_sim_caches(sim_id)


def test_start_simulation_sets_absolute_per_sim_oasis_db_path(prepared_sim):
    sim_id, sim_dir, captured = prepared_sim

    SimulationRunner.start_simulation(sim_id)

    db_path = captured["env"]["OASIS_DB_PATH"]
    expected = os.path.join(os.path.abspath(str(sim_dir)), "social_media.db")
    assert db_path == expected
    # Absolute, so OASIS (run with cwd=sim_dir) resolves it without double-nesting.
    assert os.path.isabs(db_path)
    # Parent must already exist — get_db_path() returns the value without makedirs.
    assert os.path.isdir(os.path.dirname(db_path))
    # The prior env-block vars survive the new assignment (not clobbered).
    assert captured["env"]["PYTHONUTF8"] == "1"


def test_oasis_db_path_lives_under_the_sim_cwd(prepared_sim):
    sim_id, sim_dir, captured = prepared_sim

    SimulationRunner.start_simulation(sim_id)

    # The subprocess cwd and the OASIS db share the per-sim dir → isolation.
    assert captured["cwd"] == str(sim_dir)
    assert os.path.dirname(captured["env"]["OASIS_DB_PATH"]) == os.path.abspath(str(sim_dir))


def test_oasis_db_path_is_absolutized_when_run_state_dir_is_relative(tmp_path, monkeypatch):
    """abspath() is load-bearing: production RUN_STATE_DIR is a __file__-relative
    join that can be relative, and the subprocess runs with cwd=sim_dir, so a
    relative OASIS_DB_PATH would double-nest. Drive start_simulation with a RELATIVE
    RUN_STATE_DIR and prove the env var is still absolute — this kills the
    'drop os.path.abspath' mutation, which survives the absolute-tmp_path tests."""
    monkeypatch.chdir(tmp_path)
    rel_root = os.path.join("uploads", "simulations")
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", rel_root)
    SimulationRunner._run_states.clear()
    sim_id = "sim_test_45_rel"
    sim_dir = tmp_path / "uploads" / "simulations" / sim_id
    sim_dir.mkdir(parents=True)
    _write_config(sim_dir)
    captured = _install_launch_stubs(monkeypatch)

    try:
        SimulationRunner.start_simulation(sim_id)

        db_path = captured["env"]["OASIS_DB_PATH"]
        naive_relative = os.path.join(rel_root, sim_id, "social_media.db")
        assert os.path.isabs(db_path)
        # The mutation that drops abspath would produce exactly naive_relative.
        assert db_path != naive_relative
        assert db_path == os.path.abspath(naive_relative)
        # Equals abspath(cwd) so OASIS, launched with cwd=sim_dir, resolves it in place.
        assert os.path.dirname(db_path) == os.path.abspath(captured["cwd"])
    finally:
        _drop_sim_caches(sim_id)
