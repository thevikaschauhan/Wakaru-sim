"""CodeRabbit #83 regressions through authenticated sync and queued intake."""

from copy import deepcopy

import pytest

from cart_recovery.intelligence.test_recovery import cart
from .test_buyer_state_http import payload, wire

MERCHANT = "11111111-1111-4111-8111-111111111111"
ROUTES = ["/api/cart-recovery/analyze", "/api/cart-recovery/jobs"]


def request_body():
    """Use a valid context bound to the authenticated merchant and episode."""
    body = payload()
    context = deepcopy(cart().intelligence_context)
    context.update(merchant_id=MERCHANT, episode_id=body["event_id"])
    body["intelligence_context"] = context
    return body


def prevent_work(monkeypatch):
    """Assert validation rejects before analysis or queue submission."""
    queue = wire(monkeypatch, asynchronous=True)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid intake reached paid analysis or enqueue")

    monkeypatch.setattr("app.api.cart_recovery.run_cart_recovery", unexpected)
    monkeypatch.setattr(queue, "enqueue", unexpected)
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "enforce")
    monkeypatch.setenv("TYPESAFE_RECOVERY_MERCHANTS", MERCHANT)


@pytest.mark.parametrize("route", ROUTES)
def test_invalid_server_mode_is_retryable(client, monkeypatch, route):
    prevent_work(monkeypatch)
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "typo")
    response = client.post(
        route, json=request_body(), headers={"X-Merchant-Id": MERCHANT}
    )
    assert response.status_code == 503
    assert response.json["error"] == "Intelligence mode unavailable"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("as_of", None),
        ("as_of", 42),
        ("as_of", []),
        ("feedback", [None]),
        ("feedback", ["bad"]),
        ("approved_facts", [None]),
        ("approved_facts", ["bad"]),
        ("eligible_actions", [["wait"]]),
    ],
)
def test_malformed_context_returns_400_before_work(
    client, monkeypatch, route, field, value
):
    prevent_work(monkeypatch)
    body = request_body()
    body["intelligence_context"][field] = value
    response = client.post(route, json=body, headers={"X-Merchant-Id": MERCHANT})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "case",
    [
        "missing_as_of",
        "missing_feedback_time",
        "numeric_feedback_id",
        "missing_fact_kind",
        "missing_fact_revision",
        "numeric_fact_id",
        "future_event",
        "duplicate_evidence_id",
    ],
)
def test_missing_fields_and_evidence_conflicts_rejected(
    client, monkeypatch, route, case
):
    prevent_work(monkeypatch)
    body = request_body()
    context = body["intelligence_context"]
    feedback = dict(
        id="feedback",
        text="Please help",
        source="merchant_entered",
        reporter_kind="shopper_statement",
        episode_id=body["event_id"],
        occurred_at=context["as_of"],
        recorded_at=context["as_of"],
    )
    fact = dict(
        id="fact",
        revision="1",
        kind="returns_window",
        text="30 days",
        status="active",
        scope_resolved=True,
    )
    if case == "missing_as_of":
        context.pop("as_of")
    elif case == "future_event":
        body["evidence_events"][0]["occurred_at"] = "2026-09-24T10:00:00Z"
    elif case in {
        "missing_feedback_time",
        "numeric_feedback_id",
        "duplicate_evidence_id",
    }:
        context["feedback"] = [feedback]
        if case == "missing_feedback_time":
            feedback.pop("occurred_at")
        elif case == "numeric_feedback_id":
            feedback["id"] = 7
        else:
            feedback["id"] = body["evidence_events"][0]["event_id"]
    else:
        context["approved_facts"] = [fact]
        if case == "missing_fact_kind":
            fact.pop("kind")
        elif case == "missing_fact_revision":
            fact.pop("revision")
        else:
            fact["id"] = 7
    response = client.post(route, json=body, headers={"X-Merchant-Id": MERCHANT})
    assert response.status_code == 400
