"""Tests for SimulationRunner state-persistence hardening (issue #21).

#21 was filed P0 on a cross-worker premise (status polls landing on the wrong
gunicorn worker -> stale state). That premise is moot under the current
architecture: the OASIS run executes in-process inside a single forking
``rq.Worker`` job (``backend/worker.py``) and the web tier reads RQ's
Redis-backed ``job.meta`` (``cart_recovery.py``), never ``SimulationRunner``
state. What remains real -- and is exercised here -- is the file-backed state's
robustness on the one hot paid path: corrupt-read -> FAILED (AC#5), atomic
writes, and coalescing the 2s monitor write storm.
"""
import pytest

from app.services import simulation_runner as sr
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point RUN_STATE_DIR at a temp dir and isolate the in-memory cache."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))
    SimulationRunner._run_states.clear()
    yield tmp_path
    SimulationRunner._run_states.clear()


def _write_state_file(state_dir, sim_id, content):
    sim_dir = state_dir / sim_id
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / "run_state.json").write_text(content, encoding="utf-8")


# --- corrupt-read -> FAILED, missing -> None (AC#5) -------------------------

def test_load_missing_file_returns_none(state_dir):
    assert SimulationRunner._load_run_state("no-such-sim") is None


def test_load_corrupt_file_marks_failed(state_dir):
    _write_state_file(state_dir, "sim-corrupt", "{ not valid json ]")
    state = SimulationRunner._load_run_state("sim-corrupt")
    assert state is not None, "corrupt state must not silently return None"
    assert state.runner_status == RunnerStatus.FAILED
    assert state.simulation_id == "sim-corrupt"
    assert state.error  # carries a reason


def test_get_run_state_corrupt_marks_failed(state_dir):
    # Public memory-first path: cache miss -> file fallback -> corrupt -> FAILED.
    _write_state_file(state_dir, "sim-corrupt2", "totally not json")
    state = SimulationRunner.get_run_state("sim-corrupt2")
    assert state is not None
    assert state.runner_status == RunnerStatus.FAILED


# --- atomic write ------------------------------------------------------------

def test_save_writes_valid_roundtrippable_json(state_dir):
    st = SimulationRunState(
        simulation_id="sim-ok",
        runner_status=RunnerStatus.RUNNING,
        current_round=3,
    )
    SimulationRunner._save_run_state(st)

    SimulationRunner._run_states.clear()  # force a file read
    loaded = SimulationRunner._load_run_state("sim-ok")
    assert loaded.runner_status == RunnerStatus.RUNNING
    assert loaded.current_round == 3

    # No temp scratch left behind.
    leftovers = [p.name for p in (state_dir / "sim-ok").iterdir()]
    assert leftovers == ["run_state.json"]


def test_save_is_atomic_on_replace_failure(state_dir, monkeypatch):
    good = SimulationRunState(
        simulation_id="sim-atomic",
        runner_status=RunnerStatus.RUNNING,
        current_round=1,
    )
    SimulationRunner._save_run_state(good)
    state_file = state_dir / "sim-atomic" / "run_state.json"
    prior = state_file.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(sr.os, "replace", boom)

    nxt = SimulationRunState(
        simulation_id="sim-atomic",
        runner_status=RunnerStatus.COMPLETED,
        current_round=9,
    )
    with pytest.raises(OSError):
        SimulationRunner._save_run_state(nxt)

    # Prior file untouched (no truncate-then-write) and no temp scratch leaked.
    assert state_file.read_text(encoding="utf-8") == prior
    leftovers = [p.name for p in (state_dir / "sim-atomic").iterdir()]
    assert leftovers == ["run_state.json"]


# --- coalescing the 2s write storm ------------------------------------------

def test_progress_signature_stable_for_action_churn():
    st = SimulationRunState(
        simulation_id="s", runner_status=RunnerStatus.RUNNING, current_round=2
    )
    sig = SimulationRunner._progress_signature(st)
    # Fields that churn every tick but aren't real progress must NOT change it.
    st.twitter_actions_count += 5
    st.reddit_actions_count += 3
    st.updated_at = "2026-01-01T00:00:00"
    assert SimulationRunner._progress_signature(st) == sig


def test_progress_signature_changes_on_round_advance():
    st = SimulationRunState(
        simulation_id="s", runner_status=RunnerStatus.RUNNING, current_round=2
    )
    sig = SimulationRunner._progress_signature(st)
    st.current_round = 3
    assert SimulationRunner._progress_signature(st) != sig


def test_progress_signature_changes_on_status_transition():
    st = SimulationRunState(simulation_id="s", runner_status=RunnerStatus.RUNNING)
    sig = SimulationRunner._progress_signature(st)
    st.runner_status = RunnerStatus.COMPLETED
    assert SimulationRunner._progress_signature(st) != sig


def test_monitor_loop_skips_noop_tick_writes(state_dir, monkeypatch):
    """No-op ticks (no new actions, same round/status) must not rewrite the file.

    Drives _monitor_simulation with a fake 2-tick process and no action logs, so
    the progress signature never changes during RUNNING -- the loop must persist
    zero RUNNING states and only the terminal COMPLETED transition.
    """
    monkeypatch.setattr(sr.time, "sleep", lambda _s: None)

    sim_id = "sim-coalesce"
    state = SimulationRunState(
        simulation_id=sim_id, runner_status=RunnerStatus.RUNNING, current_round=1
    )
    SimulationRunner._run_states[sim_id] = state

    class FakeProc:
        returncode = 0

        def __init__(self):
            self._polls = [None, None, 0]  # two RUNNING ticks, then exit

        def poll(self):
            return self._polls.pop(0) if self._polls else 0

    SimulationRunner._processes[sim_id] = FakeProc()

    saved_statuses = []
    orig = SimulationRunner._save_run_state.__func__

    def spy(cls, st):
        saved_statuses.append(st.runner_status)
        return orig(cls, st)

    monkeypatch.setattr(SimulationRunner, "_save_run_state", classmethod(spy))

    try:
        SimulationRunner._monitor_simulation(sim_id)
    finally:
        SimulationRunner._processes.pop(sim_id, None)

    # Zero per-tick RUNNING rewrites; exactly the terminal COMPLETED save.
    assert RunnerStatus.RUNNING not in saved_statuses
    assert saved_statuses[-1] == RunnerStatus.COMPLETED
