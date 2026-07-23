"""
In-process cart-recovery orchestrator (issue #19).

Replaces the self-HTTP loopback (the SDK ``CartRecoveryEngine`` and its HTTP
client -> ``http://localhost:5001``) with direct, synchronous calls to the service
layer. The ``/api/cart-recovery/analyze`` handler calls :func:`run_cart_recovery`
instead of routing back through the SDK, so one analyze request occupies a single
gunicorn worker (not two) and the self-DoS / worker-deadlock class is eliminated.

This is the in-process equivalent of the SDK's ``run_full_pipeline`` +
``CartRecoveryEngine.analyze_abandonment`` and preserves production behaviour
(twitter enabled, reddit disabled, ``platform='parallel'``, config-derived round
count, deterministic heuristic confidence).

Scope note: killing the loopback removes the deadlock class, but the ~8-17 min
synchronous wall-clock block REMAINS — moving analyze onto a job queue is
issue #20 and is intentionally out of scope here.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

# The exception base, not zep_cloud.ApiError (a Pydantic response model). Zep's
# typed errors (e.g. NotFoundError, status_code 404) subclass this one.
from zep_cloud.core.api_error import ApiError

from cart_recovery.email_prompt_builder import AbandonmentInsight, EmailPromptBuilder
from cart_recovery.recovery_spec import RECOVERY_REQUIREMENT, assess_confidence_heuristic
from cart_recovery.shopify_formatter import ShopifyCartData, ShopifyFormatter

from ..config import Config
from ..models.project import ProjectManager, ProjectStatus
from ..utils.llm_client import LLMClient
from ..utils.paths import SENTINEL_MERCHANT_ID
from . import graph_lifecycle
from .graph_builder import GraphBuilderService
from .ontology_generator import OntologyGenerator
from .report_agent import ReportAgent, ReportManager, ReportStatus
from .simulation_manager import SimulationManager, SimulationStatus
from .simulation_runner import RunnerStatus, SimulationRunner
from .text_processor import TextProcessor

ProgressCallback = Optional[Callable[[str, object], None]]

logger = logging.getLogger("mirofish.cart_recovery")

# Poll cadence for the OASIS run. The monitor thread advances state every ~2s
# (simulation_runner._monitor_simulation), so polling faster gains nothing.
_RUN_POLL_INTERVAL_SECONDS = 2
# Safety bound on the OASIS run wait, mirroring the timeout run_full_pipeline
# passes to wait_for_simulation for simulation_hours=24 (24 * 120 + 600). The
# real run is config-driven (~4-8 min for a 24h twitter-only sim); the gunicorn
# worker --timeout is the operational ceiling and remains the issue #20 concern.
_RUN_POLL_TIMEOUT_SECONDS = 24 * 120 + 600

_TERMINAL_STATUSES = (
    RunnerStatus.COMPLETED,
    RunnerStatus.STOPPED,
    RunnerStatus.FAILED,
)


def run_cart_recovery(
    cart: ShopifyCartData,
    on_progress: ProgressCallback = None,
    merchant_id: str = SENTINEL_MERCHANT_ID,
) -> AbandonmentInsight:
    """
    Run the full cart-recovery pipeline in-process and return an
    :class:`AbandonmentInsight`. Drop-in replacement for
    ``CartRecoveryEngine.analyze_abandonment`` that never makes an HTTP call.

    ``on_progress(stage, state)`` fires the same five stage names the SDK
    emits — ``ontology_generated``, ``graph_completed``, ``simulation_ready``,
    ``simulation_completed``, ``generating_report`` — plus ``running`` liveness
    ticks while the OASIS simulation advances.

    ``merchant_id`` attributes the run's Zep scratch graph in the lifecycle
    ledger (#72). Both live callers pass it (the /analyze handler from
    ``g.merchant_id``, the RQ job from ``job.meta``); the sentinel default
    keeps the signature backward-compatible.
    """
    # Fail fast on missing credentials before doing any work (the downstream
    # services raise mid-pipeline otherwise).
    if not Config.ZEP_API_KEY:
        raise RuntimeError("ZEP_API_KEY not configured")
    if not Config.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY not configured")

    formatter = ShopifyFormatter()
    seed_text = TextProcessor.preprocess_text(formatter.format_as_seed_doc(cart))

    # --- 1. ontology + project (in-process) ---
    # Neutral project name: do NOT embed cart.customer_id (PII) — it would land
    # in project.json, the Zep graph name, and (via on_progress) the logs.
    project = ProjectManager.create_project(name="Cart Recovery")
    # The pipeline's project/simulation/report dirs under uploads/ are write-only
    # scratch (the insight is returned, never re-read), so they are removed in the
    # finally — on success OR failure — to avoid cross-tenant co-mingling, unbounded
    # disk growth, and PII at rest (#24 CP2b). ``captured`` carries the
    # simulation_id and graph_id back out so a failure after either exists still
    # cleans it: the simulation dir is removed, and the Zep scratch graph is
    # deleted inline (#72) with its ledger record closed in the same finally.
    captured: dict = {"simulation_id": None, "graph_id": None}
    try:
        return _run_analysis(project, cart, seed_text, on_progress, captured, merchant_id)
    finally:
        _cleanup_artifacts(
            project.project_id, captured["simulation_id"], captured["graph_id"], merchant_id
        )


def _run_analysis(
    project,
    cart: ShopifyCartData,
    seed_text: str,
    on_progress: ProgressCallback,
    captured: dict,
    merchant_id: str,
) -> AbandonmentInsight:
    """Run the pipeline body for an already-created ``project``.

    Extracted from run_cart_recovery so the caller can wrap it in a try/finally
    that removes the per-analysis scratch (#24 CP2b). ``captured["simulation_id"]``
    and ``captured["graph_id"]`` are set the moment each exists so a
    mid-pipeline failure still gets its directory cleaned up and its Zep graph
    deleted (#72). ``merchant_id`` attributes the graph in the lifecycle ledger.
    """
    prompt_builder = EmailPromptBuilder()
    project.simulation_requirement = RECOVERY_REQUIREMENT
    project.total_text_length = len(seed_text)
    ProjectManager.save_extracted_text(project.project_id, seed_text)

    ontology = OntologyGenerator().generate(
        document_texts=[seed_text],
        simulation_requirement=RECOVERY_REQUIREMENT,
        additional_context=None,
    )
    # The build step expects the 2-key {entity_types, edge_types} shape; the
    # analysis_summary is stored separately.
    project.ontology = {
        "entity_types": ontology.get("entity_types", []),
        "edge_types": ontology.get("edge_types", []),
    }
    project.analysis_summary = ontology.get("analysis_summary", "")
    project.status = ProjectStatus.ONTOLOGY_GENERATED
    ProjectManager.save_project(project)
    if on_progress:
        # Pass PII-free id dicts (the Project carries no cart PII, but the handler
        # logs the state at INFO — keep customer data out of the log line).
        on_progress("ontology_generated", {"project_id": project.project_id})

    # --- 2. build graph (in-process, synchronous) ---
    builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)

    def _persist_graph_id(graph_id: str) -> None:
        # Persist graph_id the moment the graph exists, so it survives a failure
        # in a later step (mirrors the route's mid-build save). ``captured``
        # feeds the finally's inline Zep delete (#72), and the ledger "created"
        # record lands here too - record_created is internally guarded so a
        # Redis outage degrades to a warning, never a failed analysis.
        captured["graph_id"] = graph_id
        project.graph_id = graph_id
        ProjectManager.save_project(project)
        graph_lifecycle.record_created(graph_id, merchant_id)

    build_result = builder.build_graph_sync(
        text=seed_text,
        ontology=project.ontology,
        graph_name=(project.name or "MiroFish Graph"),
        on_graph_created=_persist_graph_id,
    )
    graph_id = build_result["graph_id"]
    project.status = ProjectStatus.GRAPH_COMPLETED
    ProjectManager.save_project(project)
    if on_progress:
        on_progress("graph_completed", {"project_id": project.project_id, "graph_id": graph_id})

    # --- 3 + 4. create + prepare simulation ---
    manager = SimulationManager()
    sim_state = manager.create_simulation(
        project_id=project.project_id,
        graph_id=graph_id,
        enable_twitter=True,
        enable_reddit=False,
    )
    simulation_id = sim_state.simulation_id
    captured["simulation_id"] = simulation_id

    # Preserve the prepare-dedup gate the HTTP route performs. A freshly created
    # simulation_id has no simulation_config.json and state.json status "created",
    # so this returns (False, ...) and prepare runs. The gate only short-circuits
    # a genuinely re-submitted, already-config-generated simulation_id.
    is_prepared, _ = SimulationManager._check_simulation_prepared(simulation_id)
    if not is_prepared:
        prepared = manager.prepare_simulation(
            simulation_id=simulation_id,
            simulation_requirement=RECOVERY_REQUIREMENT,
            document_text=seed_text,
            defined_entity_types=None,
            use_llm_for_profiles=True,
            progress_callback=None,
            parallel_profile_count=5,  # HTTP route default (service default is 3)
        )
        # prepare_simulation sets FAILED and RETURNS (does not raise) when no
        # entities survive filtering.
        if prepared.status == SimulationStatus.FAILED:
            raise RuntimeError(
                f"simulation prepare failed: {simulation_id} ({prepared.error})"
            )
    if on_progress:
        on_progress("simulation_ready", {"simulation_id": simulation_id})

    # --- 5. start + poll the OASIS subprocess (request-scoped cleanup) ---
    SimulationRunner.start_simulation(
        simulation_id=simulation_id,
        platform="parallel",  # SDK sends no platform -> route default "parallel"
        max_rounds=None,      # SDK sends none -> config-derived total_rounds
        enable_graph_memory_update=False,
        graph_id=None,
    )
    try:
        run_state = _wait_for_run(simulation_id, on_progress)
    finally:
        _safe_stop(simulation_id)

    if run_state.runner_status == RunnerStatus.FAILED:
        raise RuntimeError(f"simulation failed: {simulation_id} ({run_state.error})")
    if on_progress:
        on_progress("simulation_completed",
                    {"simulation_id": simulation_id, "runner_status": run_state.runner_status.value})

    # --- 6 + 7. report (in-process, synchronous) ---
    report_content = _generate_or_reuse_report(
        simulation_id, graph_id, RECOVERY_REQUIREMENT, on_progress
    )

    # --- tail: confidence + insight (mirrors analyze_abandonment) ---
    # Inject the structured-insight extractor (#47): build() makes one JSON LLM
    # call for the fragile prose fields, falling back to the regex heuristics on
    # any failure so a provider blip never loses the paid analysis (Fork D).
    # Construct the client here, outside build()'s fail-safe, and degrade to
    # heuristics (chat_json=None) if even construction fails — so a credentials
    # hiccup at the tail of an 8-17 min run still returns a result, not a raise.
    confidence, confidence_reasoning = assess_confidence_heuristic(cart)
    try:
        insight_extractor = LLMClient().chat_json
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the paid analysis
        logger.warning(
            "LLM client unavailable (%s); insight extraction will use heuristics",
            type(exc).__name__,
        )
        insight_extractor = None
    insight = prompt_builder.build(
        report_content, cart, confidence=confidence,
        chat_json=insight_extractor,
    )
    insight.confidence_reasoning = confidence_reasoning
    return insight


def _cleanup_artifacts(
    project_id: str,
    simulation_id: Optional[str],
    graph_id: Optional[str] = None,
    merchant_id: str = SENTINEL_MERCHANT_ID,
) -> None:
    """Best-effort removal of one analysis's scratch artifacts (#24 CP2b, #72).

    Three local dirs hold a run's artifacts: the project, the simulation (state,
    config, actions.jsonl, OASIS outputs, and run-state all share one
    uploads/simulations/<id> tree), and the report. A fourth artifact lives in
    Zep Cloud: the run's throwaway knowledge graph, deleted inline here (#72,
    fast path) with the lifecycle ledger updated; the W2 sweeper catches any
    graph this misses. Each removal is guarded so a cleanup error never masks
    the analysis result or its propagating exception. The report id is resolved
    first - it is keyed under uploads/reports, linked by simulation_id - so
    deleting the simulation dir cannot affect the lookup. ids
    (proj_/sim_/report_/mirofish_) are random hex, not PII, so logging them is
    safe. The graph is only ever deleted after _run_analysis has returned (the
    insight is already built; ReportAgent is the last graph reader), so this
    fast path cannot race its own run.
    """
    report_id = None
    if simulation_id is not None:
        try:
            report = ReportManager.get_report_by_simulation(simulation_id)
            report_id = report.report_id if report is not None else None
        except Exception:
            logger.warning("scratch cleanup: report lookup failed for %s", simulation_id)

    try:
        ProjectManager.delete_project(project_id)
    except Exception:
        logger.warning("scratch cleanup: project delete failed for %s", project_id)

    if simulation_id is not None:
        try:
            SimulationManager.delete_simulation(simulation_id)
        except Exception:
            logger.warning("scratch cleanup: simulation delete failed for %s", simulation_id)

    if report_id is not None:
        try:
            ReportManager.delete_report(report_id)
        except Exception:
            logger.warning("scratch cleanup: report delete failed for %s", report_id)

    if graph_id is not None:
        # `deleted` gates ledger closure. It is set both when the delete
        # succeeds and when Zep reports the graph already gone (404): an
        # already-absent graph is the successful end state, and leaving the
        # ledger record live would strand the id in the active/merchant sets
        # with no TTL (issue-72 TDD V-1). record_deleted runs OUTSIDE the try
        # so a ledger hiccup is never mislabeled as a Zep delete failure.
        deleted = False
        try:
            GraphBuilderService(api_key=Config.ZEP_API_KEY).delete_graph(graph_id)
            deleted = True
        except ApiError as exc:
            if getattr(exc, "status_code", None) == 404:
                deleted = True
            else:
                logger.warning(
                    "scratch cleanup: zep graph delete failed for %s (m=%s): %s",
                    graph_id, merchant_id, exc,
                )
        except Exception:
            # Best-effort fast path: the W2 sweeper deletes what this misses
            # within one TTL window, so a Zep hiccup here is a warning, not a
            # failure. merchant_id is logged for attribution (#24 log style).
            logger.warning(
                "scratch cleanup: zep graph delete failed for %s (m=%s)", graph_id, merchant_id
            )
        if deleted:
            graph_lifecycle.record_deleted(graph_id, source="inline")


def _wait_for_run(simulation_id: str, on_progress: ProgressCallback):
    """Block until the OASIS run reaches a terminal status.

    Emits a "running" heartbeat per round-advance (not per poll-tick like the
    SDK's 30s cadence) — meaningful liveness without flooding the log.
    """
    elapsed = 0
    last_round = -1
    while True:
        run_state = SimulationRunner.get_run_state(simulation_id)
        if run_state is None:
            raise RuntimeError(f"run state missing for {simulation_id}")
        if on_progress and run_state.current_round != last_round:
            on_progress("running", {"simulation_id": simulation_id, "current_round": run_state.current_round})
            last_round = run_state.current_round
        if run_state.runner_status in _TERMINAL_STATUSES:
            return run_state
        if elapsed >= _RUN_POLL_TIMEOUT_SECONDS:
            raise TimeoutError(
                f"simulation {simulation_id} exceeded {_RUN_POLL_TIMEOUT_SECONDS}s"
            )
        time.sleep(_RUN_POLL_INTERVAL_SECONDS)
        elapsed += _RUN_POLL_INTERVAL_SECONDS


def _safe_stop(simulation_id: str) -> None:
    """Reap the OASIS subprocess group iff still alive; never mask the real result."""
    try:
        run_state = SimulationRunner.get_run_state(simulation_id)
        if run_state is not None and run_state.runner_status in (
            RunnerStatus.RUNNING,
            RunnerStatus.PAUSED,
        ):
            SimulationRunner.stop_simulation(simulation_id)
    except Exception:
        # Best-effort cleanup invoked from the caller's finally. A stop error
        # here — ValueError when the run is already terminal (the normal
        # completion path), or an OSError while stop_simulation persists state —
        # must NEVER replace the real analyze result or the original exception
        # propagating through the finally. So swallow everything.
        pass


def _generate_or_reuse_report(
    simulation_id: str,
    graph_id: str,
    requirement: str,
    on_progress: ProgressCallback,
) -> str:
    """Return the report markdown, reusing an already-COMPLETED report (dedup)."""
    existing = ReportManager.get_report_by_simulation(simulation_id)
    if existing is not None and existing.status == ReportStatus.COMPLETED:
        return existing.markdown_content

    if on_progress:
        on_progress("generating_report", {"simulation_id": simulation_id})

    agent = ReportAgent(
        graph_id=graph_id,
        simulation_id=simulation_id,
        simulation_requirement=requirement,
    )
    report = agent.generate_report()
    # generate_report returns a FAILED report instead of raising.
    if report.status != ReportStatus.COMPLETED:
        raise RuntimeError(
            f"report generation failed: {simulation_id} ({report.error})"
        )
    return report.markdown_content
