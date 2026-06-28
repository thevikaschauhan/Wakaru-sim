"""
Issue #19 — unit tests for the in-process cart-recovery orchestrator
(``backend/app/services/cart_recovery_workflow.py``).

The orchestrator replaces the self-HTTP loopback with direct service calls.
These tests stub the heavy services (LLM / Zep / OASIS subprocess) and assert
the orchestration PROPERTIES rather than literal output: pipeline order, the
five progress stages, both idempotency gates, fail-fast on prepare/run failure,
request-scoped subprocess cleanup, per-call isolation under concurrency, and a
mirofish-free import graph (AC#1, at the import level).
"""
import os
import subprocess
import sys
import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Config
from app.services import cart_recovery_workflow as wf
from app.services.report_agent import ReportStatus
from app.services.simulation_manager import SimulationStatus
from app.services.simulation_runner import RunnerStatus
from cart_recovery.email_prompt_builder import AbandonmentInsight
from cart_recovery.recovery_spec import assess_confidence_heuristic
from cart_recovery.shopify_formatter import ShopifyCartData


def _make_cart(customer_id="cust_x"):
    return ShopifyCartData(
        customer_id=customer_id,
        customer_name="Shopper",
        email="shopper@example.com",
        cart_items=[{"product": "Widget", "price": 20.0, "quantity": 1}],
        cart_total=20.0,
        shopper_profile={"signal": 1},        # bumps the heuristic above baseline
        form_interactions=[{"field": "email"}],
    )


class _FakeProject:
    def __init__(self, name="cart_test"):
        self.project_id = "proj_test"
        self.name = name
        self.graph_id = None
        self.status = None
        self.simulation_requirement = ""
        self.total_text_length = 0
        self.ontology = None
        self.analysis_summary = ""


@pytest.fixture
def harness(monkeypatch):
    """Stub every heavy service the orchestrator calls; record call order + knobs."""
    # Config reads env at import time, so set the class attrs the orchestrator checks.
    monkeypatch.setattr(Config, "ZEP_API_KEY", "test")
    monkeypatch.setattr(Config, "LLM_API_KEY", "test")

    calls = []
    state = SimpleNamespace(
        prepare_status=SimulationStatus.READY,
        run_sequence=[RunnerStatus.RUNNING, RunnerStatus.COMPLETED],
        already_prepared=False,
        existing_report=None,
        report_status=ReportStatus.COMPLETED,
        sim_id="sim_test",
    )
    run_idx = count()

    class FakeProjectManager:
        @classmethod
        def create_project(cls, name="x"):
            calls.append("create_project")
            return _FakeProject(name=name)

        @classmethod
        def save_project(cls, project):
            calls.append("save_project")

        @classmethod
        def save_extracted_text(cls, project_id, text):
            calls.append("save_extracted_text")

    class FakeOntologyGenerator:
        def generate(self, document_texts, simulation_requirement, additional_context=None):
            calls.append("ontology")
            return {"entity_types": [{"name": "Person"}], "edge_types": [], "analysis_summary": "s"}

    class FakeBuilder:
        def __init__(self, api_key=None):
            pass

        def build_graph_sync(self, text, ontology, graph_name, on_graph_created=None, **kw):
            calls.append("build")
            if on_graph_created:
                on_graph_created("graph_test")
            return {"graph_id": "graph_test", "graph_data": {}, "node_count": 1, "edge_count": 1, "chunk_count": 1}

    class FakeManager:
        @staticmethod
        def _check_simulation_prepared(simulation_id):
            calls.append("check_prepared")
            return (state.already_prepared, {})

        def create_simulation(self, project_id, graph_id, enable_twitter=True, enable_reddit=True):
            calls.append("create_sim")
            return SimpleNamespace(simulation_id=state.sim_id, project_id=project_id, status=SimulationStatus.CREATED)

        def prepare_simulation(self, simulation_id, simulation_requirement, document_text, **kw):
            calls.append("prepare")
            return SimpleNamespace(status=state.prepare_status, error="prepare boom")

    class FakeRunner:
        stop_calls = []

        @classmethod
        def start_simulation(cls, simulation_id, **kw):
            calls.append("start")

        @classmethod
        def get_run_state(cls, simulation_id):
            i = next(run_idx)
            seq = state.run_sequence
            status = seq[min(i, len(seq) - 1)]
            return SimpleNamespace(runner_status=status, current_round=i, error="run boom")

        @classmethod
        def stop_simulation(cls, simulation_id):
            cls.stop_calls.append(simulation_id)

    class FakeReportManager:
        @classmethod
        def get_report_by_simulation(cls, simulation_id):
            calls.append("report_dedup")
            return state.existing_report

    class FakeReportAgent:
        def __init__(self, graph_id, simulation_id, simulation_requirement):
            pass

        def generate_report(self, **kw):
            calls.append("report_gen")
            return SimpleNamespace(status=state.report_status, markdown_content="# Report", error="report boom")

    class FakeLLMClient:
        """Stub the structured-insight extractor (#47). chat_json returns a valid
        extraction dict so the real EmailPromptBuilder runs its LLM path without
        a network call; the messages it receives are recorded for assertions.

        Defined inside this function-scoped fixture, so the class-level ``calls``
        list (mirroring FakeRunner.stop_calls above) is fresh for each test."""
        calls = []

        def __init__(self, *a, **kw):
            pass

        def chat_json(self, messages, *a, **kw):
            FakeLLMClient.calls.append(messages)
            return {
                "predicted_reason": "stubbed reason",
                "emotional_state": "indecisive",
                "key_objections": ["stub objection"],
            }

    monkeypatch.setattr(wf, "ProjectManager", FakeProjectManager)
    monkeypatch.setattr(wf, "OntologyGenerator", FakeOntologyGenerator)
    monkeypatch.setattr(wf, "GraphBuilderService", FakeBuilder)
    monkeypatch.setattr(wf, "SimulationManager", FakeManager)
    monkeypatch.setattr(wf, "SimulationRunner", FakeRunner)
    monkeypatch.setattr(wf, "ReportManager", FakeReportManager)
    monkeypatch.setattr(wf, "ReportAgent", FakeReportAgent)
    monkeypatch.setattr(wf, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(wf, "_RUN_POLL_INTERVAL_SECONDS", 0)
    return SimpleNamespace(calls=calls, state=state, runner=FakeRunner, llm=FakeLLMClient)


def test_returns_insight_and_fires_five_stages(harness):
    stages = []
    insight = wf.run_cart_recovery(_make_cart(), on_progress=lambda s, st: stages.append(s))

    assert isinstance(insight, AbandonmentInsight)
    assert 0.0 <= insight.confidence <= 1.0
    assert insight.confidence_reasoning  # heuristic reasoning threaded through

    milestones = [s for s in stages if s in (
        "ontology_generated", "graph_completed", "simulation_ready",
        "simulation_completed", "generating_report")]
    assert milestones == [
        "ontology_generated", "graph_completed",
        "simulation_ready", "simulation_completed", "generating_report",
    ]


def test_llm_extractor_is_passed_to_builder(harness):
    """#47: the workflow injects a chat_json extractor into EmailPromptBuilder,
    so the structured-insight LLM path runs (not the regex heuristics)."""
    insight = wf.run_cart_recovery(_make_cart())
    assert harness.llm.calls, "expected the injected chat_json extractor to be invoked"
    assert insight.predicted_reason == "stubbed reason"


def test_pipeline_call_order(harness):
    wf.run_cart_recovery(_make_cart())
    order = [c for c in harness.calls
             if c in ("ontology", "build", "create_sim", "prepare", "start", "report_gen")]
    assert order == ["ontology", "build", "create_sim", "prepare", "start", "report_gen"]


def test_progress_payloads_are_pii_free_dicts(harness):
    """on_progress payloads must be small id dicts, never domain objects that
    could carry cart PII (e.g. customer_id) into the handler's INFO logs."""
    cart = _make_cart(customer_id="cust_secret_pii")
    states = []
    wf.run_cart_recovery(cart, on_progress=lambda s, st: states.append(st))

    assert states, "expected progress callbacks to fire"
    for st in states:
        assert isinstance(st, dict), f"progress payload should be a dict, got {type(st)}"
    blob = " ".join(repr(st) for st in states)
    assert "cust_secret_pii" not in blob   # customer_id must not reach progress payloads
    assert "shopper@example.com" not in blob


def test_report_dedup_skips_generation(harness):
    harness.state.existing_report = SimpleNamespace(
        status=ReportStatus.COMPLETED, markdown_content="# Cached")
    wf.run_cart_recovery(_make_cart())
    assert "report_gen" not in harness.calls  # COMPLETED report reused, not regenerated


def test_prepare_dedup_short_circuits_prepare(harness):
    """When _check_simulation_prepared reports True, prepare is skipped (the other
    idempotency gate). Defensive in production — each run mints a fresh sim — but
    the docstring promises both gates, so exercise the short-circuit branch."""
    harness.state.already_prepared = True
    insight = wf.run_cart_recovery(_make_cart())
    assert "prepare" not in harness.calls   # already-prepared sim skips prepare
    assert "start" in harness.calls          # pipeline still proceeds
    assert isinstance(insight, AbandonmentInsight)


def test_prepare_failure_raises_before_run(harness):
    harness.state.prepare_status = SimulationStatus.FAILED
    with pytest.raises(RuntimeError, match="prepare failed"):
        wf.run_cart_recovery(_make_cart())
    assert "start" not in harness.calls  # never launched the OASIS run


def test_run_failure_raises_before_report(harness):
    harness.state.run_sequence = [RunnerStatus.FAILED]
    with pytest.raises(RuntimeError, match="simulation failed"):
        wf.run_cart_recovery(_make_cart())
    assert "report_gen" not in harness.calls  # never generated a report on a failed run


def test_no_stop_on_normal_completion(harness):
    wf.run_cart_recovery(_make_cart())
    assert harness.runner.stop_calls == []  # COMPLETED -> _safe_stop is a no-op


def test_scratch_cleanup_fires_on_success(harness, monkeypatch):
    # #24 CP2b: the per-analysis scratch is removed after a successful run, with
    # both the project and simulation ids.
    cleaned = []
    monkeypatch.setattr(wf, "_cleanup_artifacts", lambda pid, sid: cleaned.append((pid, sid)))
    wf.run_cart_recovery(_make_cart())
    assert cleaned == [("proj_test", "sim_test")]


def test_scratch_cleanup_fires_on_failure_after_sim(harness, monkeypatch):
    # A failure AFTER the simulation is created must still clean its dir —
    # captured["simulation_id"] is set the moment the sim exists, so the finally
    # passes it through.
    harness.state.run_sequence = [RunnerStatus.FAILED]
    cleaned = []
    monkeypatch.setattr(wf, "_cleanup_artifacts", lambda pid, sid: cleaned.append((pid, sid)))
    with pytest.raises(RuntimeError, match="simulation failed"):
        wf.run_cart_recovery(_make_cart())
    assert cleaned == [("proj_test", "sim_test")]


def test_scratch_cleanup_fires_on_early_failure_without_sim(harness, monkeypatch):
    # A failure BEFORE the simulation is created cleans the project only (the sim
    # id is still None).
    cleaned = []
    monkeypatch.setattr(wf, "_cleanup_artifacts", lambda pid, sid: cleaned.append((pid, sid)))

    def boom(self, document_texts, simulation_requirement, additional_context=None):
        raise RuntimeError("ontology boom")

    monkeypatch.setattr(wf.OntologyGenerator, "generate", boom)
    with pytest.raises(RuntimeError, match="ontology boom"):
        wf.run_cart_recovery(_make_cart())
    assert cleaned == [("proj_test", None)]


def test_subprocess_cleanup_on_timeout(harness, monkeypatch):
    harness.state.run_sequence = [RunnerStatus.RUNNING]   # never terminates
    monkeypatch.setattr(wf, "_RUN_POLL_TIMEOUT_SECONDS", 0)  # force immediate timeout
    with pytest.raises(TimeoutError):
        wf.run_cart_recovery(_make_cart())
    assert harness.runner.stop_calls == [harness.state.sim_id]  # finally reaped the live run


def test_safe_stop_guards_terminal_status(monkeypatch):
    # live run -> stop is called
    stopped = []

    class LiveRunner:
        @classmethod
        def get_run_state(cls, sid):
            return SimpleNamespace(runner_status=RunnerStatus.RUNNING)

        @classmethod
        def stop_simulation(cls, sid):
            stopped.append(sid)

    monkeypatch.setattr(wf, "SimulationRunner", LiveRunner)
    wf._safe_stop("sim_live")
    assert stopped == ["sim_live"]

    # terminal run -> stop_simulation raises ValueError (normal-completion path) -> swallowed
    class DoneRunner:
        @classmethod
        def get_run_state(cls, sid):
            return SimpleNamespace(runner_status=RunnerStatus.COMPLETED)

        @classmethod
        def stop_simulation(cls, sid):
            raise ValueError("not running")

    monkeypatch.setattr(wf, "SimulationRunner", DoneRunner)
    wf._safe_stop("sim_done")  # must not raise


def test_concurrent_runs_isolated(monkeypatch):
    """Two concurrent runs are genuinely in-flight at once and stay isolated.

    A Barrier(2) inside the stubbed start_simulation forces both runs to reach
    that point simultaneously — if one blocked the other, the barrier would time
    out (BrokenBarrierError) and fail the test. This validates AC#2's "do not
    block each other" intent on the in-process path, and asserts distinct
    simulation_ids (no shared-state cross-talk). A live `hey -c 2 -n 4` would
    instead exercise the out-of-scope 8-17 min OASIS subprocess + paid LLM/Zep
    calls, so the stubbed barrier is the calibrated substitute (see #20)."""
    monkeypatch.setattr(Config, "ZEP_API_KEY", "test")
    monkeypatch.setattr(Config, "LLM_API_KEY", "test")
    sim_counter = count()
    created = []
    lock = threading.Lock()
    both_started = threading.Barrier(2, timeout=8)

    class FakePM:
        @classmethod
        def create_project(cls, name="x"):
            return _FakeProject(name=name)

        @classmethod
        def save_project(cls, project):
            pass

        @classmethod
        def save_extracted_text(cls, pid, text):
            pass

    class FakeOnto:
        def generate(self, document_texts, simulation_requirement, additional_context=None):
            return {"entity_types": [{"name": "P"}], "edge_types": [], "analysis_summary": ""}

    class FakeBuilder:
        def __init__(self, api_key=None):
            pass

        def build_graph_sync(self, text, ontology, graph_name, on_graph_created=None, **kw):
            if on_graph_created:
                on_graph_created("g")
            return {"graph_id": "g", "graph_data": {}, "node_count": 0, "edge_count": 0, "chunk_count": 0}

    class FakeMgr:
        @staticmethod
        def _check_simulation_prepared(sid):
            return (False, {})

        def create_simulation(self, project_id, graph_id, enable_twitter=True, enable_reddit=True):
            with lock:
                sid = f"sim_{next(sim_counter)}"
                created.append(sid)
            return SimpleNamespace(simulation_id=sid, project_id=project_id, status=SimulationStatus.CREATED)

        def prepare_simulation(self, simulation_id, simulation_requirement, document_text, **kw):
            return SimpleNamespace(status=SimulationStatus.READY, error=None)

    class FakeRunner:
        @classmethod
        def start_simulation(cls, simulation_id, **kw):
            # Both runs must reach here concurrently; if one serialized behind the
            # other, this barrier raises BrokenBarrierError and fails the test.
            both_started.wait()

        @classmethod
        def get_run_state(cls, sid):
            return SimpleNamespace(runner_status=RunnerStatus.COMPLETED, current_round=1, error=None)

        @classmethod
        def stop_simulation(cls, sid):
            pass

    class FakeRM:
        @classmethod
        def get_report_by_simulation(cls, sid):
            return None

    class FakeRA:
        def __init__(self, graph_id, simulation_id, simulation_requirement):
            self.sid = simulation_id

        def generate_report(self, **kw):
            return SimpleNamespace(status=ReportStatus.COMPLETED, markdown_content=f"# {self.sid}", error=None)

    class FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, messages, *a, **kw):
            return {"predicted_reason": "r", "emotional_state": "indecisive", "key_objections": []}

    for name, obj in [
        ("ProjectManager", FakePM), ("OntologyGenerator", FakeOnto),
        ("GraphBuilderService", FakeBuilder), ("SimulationManager", FakeMgr),
        ("SimulationRunner", FakeRunner), ("ReportManager", FakeRM),
        ("ReportAgent", FakeRA), ("LLMClient", FakeLLM),
    ]:
        monkeypatch.setattr(wf, name, obj)
    monkeypatch.setattr(wf, "_RUN_POLL_INTERVAL_SECONDS", 0)

    results, errors = {}, {}

    def worker(k):
        try:
            results[k] = wf.run_cart_recovery(_make_cart(customer_id=f"c{k}"))
        except Exception as e:  # noqa: BLE001 - surface any thread error to the assert
            errors[k] = e

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(results) == 2
    assert all(isinstance(r, AbandonmentInsight) for r in results.values())
    assert len(set(created)) == 2  # distinct simulation_ids; no cross-contamination


def test_assess_confidence_heuristic_arithmetic():
    """Direct guard on the relocated heuristic's score arithmetic + the min(.,1.0)
    cap (shared verbatim by the SDK engine and the in-process orchestrator)."""
    bare = ShopifyCartData(
        customer_id="c", customer_name="n", email="e@example.com",
        cart_items=[], cart_total=0.0)
    score, reason = assess_confidence_heuristic(bare)
    assert score == 0.3          # baseline, no signals
    assert "minimal" in reason

    full = ShopifyCartData(
        customer_id="c", customer_name="n", email="e@example.com",
        cart_items=[], cart_total=0.0,
        shopper_profile={"x": 1},          # +0.15
        behavioral_memory="m",             # +0.10
        form_interactions=[{}],            # +0.10
        hover_signals=[{}],                # +0.10
        ontology_hint="h",                 # +0.15
        recovery_history=[{}],             # +0.10
        alert_messages_shown=["a"],        # +0.05
        searches_submitted=["s"],          # +0.05
    )
    score, reason = assess_confidence_heuristic(full)
    assert score == 1.0          # 0.3 + 1.10 raw -> capped at 1.0
    assert "minimal" not in reason


def test_orchestrator_import_graph_is_mirofish_free():
    """Importing the orchestrator must NOT pull mirofish / cart_recovery.engine (AC#1)."""
    repo_root = Path(__file__).resolve().parents[2]
    backend = repo_root / "backend"
    code = (
        "import sys\n"
        "import app.services.cart_recovery_workflow\n"
        "assert 'mirofish' not in sys.modules, 'mirofish leaked into backend import graph'\n"
        "assert 'cart_recovery.engine' not in sys.modules, 'cart_recovery.engine leaked'\n"
        "print('clean')\n"
    )
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(backend), str(repo_root)]),
        LLM_API_KEY="test",
        ZEP_API_KEY="test",
        SECRET_KEY="test",
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert "clean" in proc.stdout
