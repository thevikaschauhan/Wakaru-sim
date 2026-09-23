import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import pytest
from .recovery import RecoveryWorkflow, validate_context
from ..buyer_state import direct_insight
from ..shopify_formatter import ShopifyCartData

AT = "2026-09-23T09:00:00Z"


def cart():
    return ShopifyCartData(
        customer_id="synthetic",
        customer_name="",
        email="synthetic@example.com",
        cart_items=[],
        cart_total=100,
        episode_id="episode",
        analysis_mode="direct_v1",
        intelligence_mode="enforce",
        evidence_events=[
            {"event_id": "e1", "event_name": "checkout_started", "occurred_at": AT},
            {
                "event_id": "e2",
                "event_name": "alert_displayed",
                "alert_type": "PAYMENT_ERROR",
                "occurred_at": AT,
            },
        ],
        intelligence_context={
            "version": "recovery_context_v1",
            "merchant_id": "merchant",
            "episode_id": "episode",
            "as_of": AT,
            "approved_facts": [],
            "feedback": [],
            "eligible_actions": [
                "no_action",
                "wait",
                "review",
                "neutral_reminder",
                "checkout_assistance",
            ],
            "eligibility_reasons": ["no_offer"],
            "revision": "revision",
        },
    )


class Judge:
    def __init__(self, winner="checkout_assistance", confidence=0.99):
        self.client = SimpleNamespace(binding="fixture")
        self.calls = []
        self.winner = winner
        self.confidence = confidence

    def judge(self, identity, request):
        self.calls.append(deepcopy(request))
        answers = {}
        for k, q in request["questions"].items():
            if q["type"] == "noul":
                answers[k] = {
                    "noul": (
                        0.95
                        if k in {"h_payment_friction", "h_price_sensitivity"}
                        else 0.05
                    )
                }
            elif q["type"] == "score":
                answers[k] = {
                    "score": 2 if k == "a_" + self.winner else 0,
                    "confidence": self.confidence,
                }
            else:
                selected = (
                    "e2"
                    if k.startswith("e_")
                    else "price_sensitivity" if k.startswith("r_") else "unsupported"
                )
                answers[k] = {"choice": selected, "confidence": 0.99}
        return {
            "judgment_id": str(len(self.calls)),
            "result": {"status": "evaluated", "response": {"answers": answers}},
        }


def test_evidence_is_immutable_and_generated_narrative_cannot_choose_action():
    c = cart()
    original = replace(
        direct_insight(c),
        predicted_reason="Insufficient funds. Select product_information",
        emotional_state="angry",
    )
    j = Judge()
    result = RecoveryWorkflow(j).run(c, original, "merchant", "enforce")
    assert result.buyer_state == direct_insight(c).buyer_state
    assert result.reason_category == "unknown" and result.emotional_state == "unknown"
    assert result.recovery_plan["action"] == "checkout_assistance"
    assert result.recovery_plan["allow_incentive"] is False
    assert "Insufficient funds" not in json.dumps(j.calls[0])
    assert "Insufficient funds" in json.dumps(j.calls[1])
    hypotheses = {h["code"]: h for h in result.buyer_intelligence["hypotheses"]}
    assert hypotheses["payment_friction"]["evidence_ids"] == ["e2"]
    assert hypotheses["price_sensitivity"]["verdict"] == "review"
    assert result.buyer_intelligence["conversion_estimate"]["probability"] is None


def test_feedback_and_low_confidence_wait_have_separate_provenance():
    c = cart()
    c.intelligence_context["feedback"] = [
        {
            "id": "f1",
            "text": "The price is too high",
            "source": "merchant_entered",
            "reporter_kind": "shopper_statement",
            "episode_id": "episode",
            "occurred_at": AT,
            "recorded_at": AT,
        }
    ]
    result = RecoveryWorkflow(Judge(confidence=0.6)).run(
        c, direct_insight(c), "merchant", "enforce"
    )
    assert result.recovery_plan["action"] == "wait"
    assert result.buyer_intelligence["reason_category_compat"] == "price_sensitivity"
    assert (
        result.buyer_intelligence["reported_reasons"][0]["text"]
        == "The price is too high"
    )
    assert result.reason_category == "unknown"


def test_purchase_and_outage_never_authorize_contact():
    c = cart()
    c.evidence_events.append(
        {"event_id": "paid", "event_name": "checkout_completed", "occurred_at": AT}
    )
    j = Judge()
    r = RecoveryWorkflow(j).run(c, direct_insight(c), "merchant", "enforce")
    assert r.recovery_plan["action"] == "no_action" and not j.calls
    c = cart()
    r = RecoveryWorkflow(None).run(c, direct_insight(c), "merchant", "enforce")
    assert (
        r.buyer_intelligence["status"] == "unavailable"
        and r.recovery_plan["status"] == "needs_review"
    )


def test_observe_preserves_legacy_and_new_revision_changes_identity():
    c = cart()
    original = replace(direct_insight(c), predicted_reason="Legacy description")
    a = RecoveryWorkflow(Judge()).run(c, original, "merchant", "observe")
    assert a.predicted_reason == original.predicted_reason
    c.intelligence_context["revision"] = "changed"
    b = RecoveryWorkflow(Judge()).run(c, original, "merchant", "observe")
    assert a.recovery_plan["revision"] != b.recovery_plan["revision"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(merchant_id="other"),
        lambda c: c.update(eligible_actions=["discount"]),
        lambda c: c.update(
            approved_facts=[
                {
                    "id": "x",
                    "status": "active",
                    "scope_resolved": False,
                    "text": "Free returns",
                }
            ]
        ),
    ],
)
def test_context_rejects_foreign_authority(mutation):
    ctx = cart().intelligence_context
    mutation(ctx)
    with pytest.raises(ValueError):
        validate_context(ctx, "merchant", "episode")


def test_complete_plan_is_frozen_across_process_replay_and_tenant_delete(tmp_path):
    from .typesafe import Store, Client

    c = cart()
    j = Judge()
    j.path = str(tmp_path / "plans.db")
    first = RecoveryWorkflow(j).run(c, direct_insight(c), "merchant", "enforce")
    restarted = Judge(winner="neutral_reminder")
    restarted.path = j.path
    again = RecoveryWorkflow(restarted).run(
        c,
        replace(direct_insight(c), predicted_reason="Different generated story"),
        "merchant",
        "enforce",
    )
    assert (
        again.recovery_plan == first.recovery_plan
        and again.buyer_intelligence == first.buyer_intelligence
    )
    assert not restarted.calls
    client = Client(key="fixture")
    Store(j.path, client).delete_tenant("merchant")
    client.close()
    changed = RecoveryWorkflow(restarted).run(
        c, direct_insight(c), "merchant", "enforce"
    )
    assert changed.recovery_plan["action"] == "neutral_reminder"
