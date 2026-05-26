"""
Contract tests for MiroFishClient and CartRecoveryEngine.

Exercises every backend endpoint the SDK calls against a `responses`-stubbed
backend that asserts the exact URL, HTTP method, and JSON body shape. Together
with the engine-level test, this proves the SDK is wire-compatible with the
real backend without needing a live Flask process. See issue #5.
"""

from __future__ import annotations

import re

import pytest
import responses
from responses import matchers

from cart_recovery import CartRecoveryEngine, ShopifyCartData
from mirofish import MiroFishClient

BASE = "http://localhost:5001"


def _envelope(data):
    return {"success": True, "data": data}


@pytest.fixture
def cart() -> ShopifyCartData:
    return ShopifyCartData(
        customer_id="cust_test_1",
        customer_name="Test Shopper",
        email="test@example.com",
        cart_items=[
            {"product": "Wireless Headphones", "price": 99.99, "quantity": 1, "category": "Electronics"},
        ],
        cart_total=99.99,
        currency="USD",
        shipping_cost=12.99,
        shipping_method="Standard",
        exit_page="checkout/payment",
        abandoned_at_step="payment",
        device="mobile",
    )


def _stub_full_pipeline(report_markdown: str = "") -> None:
    """Register `responses` stubs for every endpoint the pipeline hits."""
    if not report_markdown:
        report_markdown = (
            "## Analysis\n\n"
            "Primary abandonment reason: shipping cost shock at checkout. "
            "The shopper showed anxiety about the total price relative to perceived value."
        )

    # 1. Ontology generate — multipart form, requires `simulation_requirement` field.
    responses.add(
        responses.POST,
        f"{BASE}/api/graph/ontology/generate",
        json=_envelope({
            "project_id": "proj_test_1",
            "ontology": {"entity_types": [], "edge_types": []},
        }),
        status=200,
    )

    # 2. Graph build — JSON body with project_id (path-less).
    responses.add(
        responses.POST,
        f"{BASE}/api/graph/build",
        json=_envelope({"project_id": "proj_test_1", "task_id": "task_build_1"}),
        status=200,
        match=[matchers.json_params_matcher({"project_id": "proj_test_1"})],
    )

    # 3. Graph build task poll — GET /api/graph/task/{task_id}.
    responses.add(
        responses.GET,
        f"{BASE}/api/graph/task/task_build_1",
        json=_envelope({
            "task_id": "task_build_1",
            "task_type": "graph_build",
            "status": "completed",
            "progress": 100,
            "result": {"graph_id": "graph_1"},
        }),
        status=200,
    )

    # 4. Fetch project after build — under /graph prefix.
    responses.add(
        responses.GET,
        f"{BASE}/api/graph/project/proj_test_1",
        json=_envelope({
            "project_id": "proj_test_1",
            "name": "Test Project",
            "status": "graph_completed",
            "graph_id": "graph_1",
        }),
        status=200,
    )

    # 5. Create simulation — JSON body, no simulation_requirement.
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/create",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "project_id": "proj_test_1",
            "status": "created",
            "enable_twitter": True,
            "enable_reddit": False,
        }),
        status=200,
        match=[matchers.json_params_matcher({
            "project_id": "proj_test_1",
            "enable_twitter": True,
            "enable_reddit": False,
        })],
    )

    # 6. Prepare simulation — JSON body with simulation_id, path-less.
    #    Stub the already_prepared shortcut so polling short-circuits.
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/prepare",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "status": "ready",
            "already_prepared": True,
        }),
        status=200,
        match=[matchers.json_params_matcher({"simulation_id": "sim_test_1"})],
    )

    # 7. Fetch simulation state after prepare.
    responses.add(
        responses.GET,
        f"{BASE}/api/simulation/sim_test_1",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "project_id": "proj_test_1",
            "status": "ready",
            "enable_twitter": True,
            "enable_reddit": False,
        }),
        status=200,
    )

    # 8. Start simulation — JSON body with simulation_id, path-less.
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/start",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "runner_status": "running",
        }),
        status=200,
        match=[matchers.json_params_matcher({"simulation_id": "sim_test_1"})],
    )

    # 9. Poll run-status — backend route uses hyphen, not underscore.
    responses.add(
        responses.GET,
        f"{BASE}/api/simulation/sim_test_1/run-status",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "runner_status": "completed",
            "current_round": 24,
            "total_rounds": 24,
            "progress_percent": 100.0,
        }),
        status=200,
    )

    # 10. Generate report — JSON body with simulation_id, path-less.
    responses.add(
        responses.POST,
        f"{BASE}/api/report/generate",
        json=_envelope({
            "simulation_id": "sim_test_1",
            "report_id": "rep_test_1",
            "task_id": "task_report_1",
            "status": "generating",
            "already_generated": False,
        }),
        status=200,
        match=[matchers.json_params_matcher({"simulation_id": "sim_test_1"})],
    )

    # 11. Poll report status — POST not GET, body has task_id + simulation_id.
    responses.add(
        responses.POST,
        f"{BASE}/api/report/generate/status",
        json=_envelope({
            "task_id": "task_report_1",
            "status": "completed",
            "progress": 100,
        }),
        status=200,
        match=[matchers.json_params_matcher({
            "task_id": "task_report_1",
            "simulation_id": "sim_test_1",
        })],
    )

    # 12. Fetch report content — keyed by simulation_id, returns markdown_content.
    responses.add(
        responses.GET,
        f"{BASE}/api/report/by-simulation/sim_test_1",
        json=_envelope({
            "report_id": "rep_test_1",
            "simulation_id": "sim_test_1",
            "status": "completed",
            "markdown_content": report_markdown,
        }),
        status=200,
    )


# ----------------------------------------------------------------------
# End-to-end test: CartRecoveryEngine.analyze_abandonment()
# ----------------------------------------------------------------------

@responses.activate
def test_cart_recovery_engine_full_pipeline(cart):
    """Walks the entire pipeline through CartRecoveryEngine — proves the SDK is wire-compatible end-to-end."""
    _stub_full_pipeline()

    engine = CartRecoveryEngine(mirofish_url=BASE, enable_reddit=False, simulation_hours=24)
    insight = engine.analyze_abandonment(cart)

    assert insight.predicted_reason, "EmailPromptBuilder should extract a reason from the report markdown"
    assert insight.emotional_state in {
        "anxious", "price-sensitive", "indecisive", "distracted",
        "comparison-shopping", "trust-lacking",
    }
    assert insight.recommended_angle
    assert insight.email_prompt_context

    # Every stubbed endpoint must have been hit at least once.
    expected_routes = {
        ("POST", f"{BASE}/api/graph/ontology/generate"),
        ("POST", f"{BASE}/api/graph/build"),
        ("GET", f"{BASE}/api/graph/task/task_build_1"),
        ("GET", f"{BASE}/api/graph/project/proj_test_1"),
        ("POST", f"{BASE}/api/simulation/create"),
        ("POST", f"{BASE}/api/simulation/prepare"),
        ("GET", f"{BASE}/api/simulation/sim_test_1"),
        ("POST", f"{BASE}/api/simulation/start"),
        ("GET", f"{BASE}/api/simulation/sim_test_1/run-status"),
        ("POST", f"{BASE}/api/report/generate"),
        ("POST", f"{BASE}/api/report/generate/status"),
        ("GET", f"{BASE}/api/report/by-simulation/sim_test_1"),
    }
    actual_routes = {(c.request.method, c.request.url.split("?")[0]) for c in responses.calls}
    missing = expected_routes - actual_routes
    assert not missing, f"Expected routes never hit: {missing}"


# ----------------------------------------------------------------------
# Ontology multipart payload uses `simulation_requirement`, not `requirement`
# ----------------------------------------------------------------------

@responses.activate
def test_generate_ontology_sends_simulation_requirement_field(tmp_path):
    seed = tmp_path / "seed.txt"
    seed.write_text("seed content")

    responses.add(
        responses.POST,
        f"{BASE}/api/graph/ontology/generate",
        json=_envelope({"project_id": "proj_x"}),
        status=200,
    )

    client = MiroFishClient(BASE)
    client.generate_ontology(files=[str(seed)], requirement="why did they leave?")

    body = responses.calls[0].request.body
    body_str = body.decode("utf-8", errors="ignore") if isinstance(body, (bytes, bytearray)) else str(body)
    assert "simulation_requirement" in body_str
    assert re.search(r'name="requirement"\b', body_str) is None, (
        "SDK must not send the legacy `requirement` form field"
    )


# ----------------------------------------------------------------------
# Envelope unwrap: SDK rejects `success: false` regardless of HTTP 200
# ----------------------------------------------------------------------

@responses.activate
def test_unwrap_raises_api_error_on_success_false():
    from mirofish.exceptions import APIError

    responses.add(
        responses.GET,
        f"{BASE}/api/graph/project/proj_x",
        json={"success": False, "error": "Project not found"},
        status=200,
    )

    client = MiroFishClient(BASE)
    with pytest.raises(APIError) as exc_info:
        client.get_project("proj_x")
    assert "Project not found" in str(exc_info.value)


# ----------------------------------------------------------------------
# chat_with_report → POST /report/chat
# ----------------------------------------------------------------------

@responses.activate
def test_chat_with_report_hits_report_chat_endpoint():
    responses.add(
        responses.POST,
        f"{BASE}/api/report/chat",
        json=_envelope({"response": "ok"}),
        status=200,
        match=[matchers.json_params_matcher({
            "simulation_id": "sim_test_1",
            "message": "why?",
            "chat_history": [],
        })],
    )

    client = MiroFishClient(BASE)
    result = client.chat_with_report("sim_test_1", "why?")
    assert result == {"response": "ok"}


# ----------------------------------------------------------------------
# interview_agent → POST /simulation/interview
# ----------------------------------------------------------------------

@responses.activate
def test_interview_agent_hits_simulation_interview_endpoint():
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/interview",
        json=_envelope({"agent_id": 0, "result": {"response": "hi"}}),
        status=200,
        match=[matchers.json_params_matcher({
            "simulation_id": "sim_test_1",
            "agent_id": 0,
            "prompt": "what do you think?",
            "timeout": 60,
            "platform": "twitter",
        })],
    )

    client = MiroFishClient(BASE)
    result = client.interview_agent("sim_test_1", agent_id=0, prompt="what do you think?", platform="twitter")
    assert result["agent_id"] == 0


# ----------------------------------------------------------------------
# stop_simulation — JSON body with simulation_id (path-less)
# ----------------------------------------------------------------------

@responses.activate
def test_stop_simulation_uses_path_less_route():
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/stop",
        json=_envelope({"simulation_id": "sim_test_1", "runner_status": "stopped"}),
        status=200,
        match=[matchers.json_params_matcher({"simulation_id": "sim_test_1"})],
    )

    client = MiroFishClient(BASE)
    client.stop_simulation("sim_test_1")


# ----------------------------------------------------------------------
# Failure-path coverage: graph build, report generate, prepare returning "failed"
# ----------------------------------------------------------------------

@responses.activate
def test_build_graph_raises_on_failed_task_status():
    from mirofish.exceptions import APIError

    responses.add(
        responses.POST,
        f"{BASE}/api/graph/build",
        json=_envelope({"project_id": "proj_x", "task_id": "task_fail_1"}),
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE}/api/graph/task/task_fail_1",
        json=_envelope({
            "task_id": "task_fail_1",
            "task_type": "graph_build",
            "status": "failed",
            "error": "Zep timed out",
        }),
        status=200,
    )

    client = MiroFishClient(BASE)
    with pytest.raises(APIError) as exc_info:
        client.build_graph("proj_x", timeout=5)
    assert "Zep timed out" in str(exc_info.value)


@responses.activate
def test_generate_report_raises_on_failed_status():
    from mirofish.exceptions import APIError

    responses.add(
        responses.POST,
        f"{BASE}/api/report/generate",
        json=_envelope({
            "simulation_id": "sim_x",
            "task_id": "task_rep_fail_1",
            "status": "generating",
            "already_generated": False,
        }),
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/report/generate/status",
        json=_envelope({
            "task_id": "task_rep_fail_1",
            "status": "failed",
            "error": "ReportAgent timed out",
        }),
        status=200,
    )

    client = MiroFishClient(BASE)
    with pytest.raises(APIError) as exc_info:
        client.generate_report("sim_x", timeout=5)
    assert "ReportAgent timed out" in str(exc_info.value)


@responses.activate
def test_prepare_simulation_raises_on_failed_status():
    from mirofish.exceptions import SimulationError

    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/prepare",
        json=_envelope({
            "simulation_id": "sim_x",
            "task_id": "task_prep_fail_1",
            "status": "preparing",
            "already_prepared": False,
        }),
        status=200,
    )
    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/prepare/status",
        json=_envelope({
            "task_id": "task_prep_fail_1",
            "status": "failed",
            "error": "profile generation crashed",
        }),
        status=200,
    )

    client = MiroFishClient(BASE)
    with pytest.raises(SimulationError) as exc_info:
        client.prepare_simulation("sim_x", timeout=5)
    assert "profile generation crashed" in str(exc_info.value)


# ----------------------------------------------------------------------
# prepare_simulation real-polling branch (already_prepared=False) + transition
# ----------------------------------------------------------------------

@responses.activate
def test_prepare_simulation_polls_until_ready(monkeypatch):
    """Covers the real polling path: prepare returns task_id, status flips from
    `processing` to `ready` across two poll calls."""
    import json
    import mirofish.polling

    # Don't actually sleep between polls.
    monkeypatch.setattr(mirofish.polling.time, "sleep", lambda _: None)

    responses.add(
        responses.POST,
        f"{BASE}/api/simulation/prepare",
        json=_envelope({
            "simulation_id": "sim_poll_1",
            "task_id": "task_prep_poll_1",
            "status": "preparing",
            "already_prepared": False,
        }),
        status=200,
        match=[matchers.json_params_matcher({"simulation_id": "sim_poll_1"})],
    )

    # Sequence the poll responses by counting calls in a callback.
    poll_count = {"n": 0}

    def poll_callback(request):
        poll_count["n"] += 1
        status = "ready" if poll_count["n"] >= 2 else "processing"
        body = _envelope({
            "task_id": "task_prep_poll_1",
            "status": status,
            "progress": 100 if status == "ready" else 40,
        })
        return 200, {"Content-Type": "application/json"}, json.dumps(body)

    responses.add_callback(
        responses.POST,
        f"{BASE}/api/simulation/prepare/status",
        callback=poll_callback,
    )

    responses.add(
        responses.GET,
        f"{BASE}/api/simulation/sim_poll_1",
        json=_envelope({
            "simulation_id": "sim_poll_1",
            "project_id": "proj_x",
            "status": "ready",
        }),
        status=200,
    )

    client = MiroFishClient(BASE)
    sim = client.prepare_simulation("sim_poll_1", timeout=30)
    assert sim.simulation_id == "sim_poll_1"
    assert poll_count["n"] == 2, f"expected 2 poll calls, got {poll_count['n']}"
