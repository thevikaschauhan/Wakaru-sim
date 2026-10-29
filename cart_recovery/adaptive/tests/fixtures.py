"""Synthetic commerce records and mock judgments, never merchant data."""

import copy
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from pathlib import Path

from cart_recovery.adaptive.planning import digest, stamp


def fixture(now=None):
    now = now or datetime.now(timezone.utc)
    rows = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts/recovery/v2/conformance.json"
        ).read_text()
    )
    c = copy.deepcopy(next(x["value"] for x in rows if x["name"] == "complete-context"))
    merchant = str(uuid4())
    journey = str(uuid4())
    c.update(
        merchant_id=merchant,
        journey_id=journey,
        abandoned_at=stamp(now - timedelta(days=1)),
        expires_at=stamp(now + timedelta(days=6)),
        cutoff_at=stamp(now),
        events=[],
        experiment_arm="typesafe_selected_followups",
    )
    c["conversation"].update(merchant_id=merchant, journey_id=journey)
    prior = {
        "subject": "Your saved cart",
        "body": "Your items are saved if you would like to return.",
    }
    h = digest(prior)
    c["conversation"]["confirmed_messages"] = [
        {
            "message_id": str(uuid4()),
            "ordinal": 1,
            "revision": 0,
            "document_ref": {"id": "first-message", "revision": 0, "sha256": h},
            "content_sha256": h,
            **prior,
            "purpose_id": "cart_reminder",
            "accepted_at": stamp(now - timedelta(hours=25)),
            "outcomes": [],
            "fact_refs": [],
            "promises": [],
            "questions": [],
            "offer_refs": [],
        }
    ]
    c["coverage"].update(full_text_checked=True, claim_extraction_complete=True)
    c["source_health"] = [
        {"source": "pixel", "status": "healthy", "observed_at": stamp(now)}
    ]
    c["allowed_windows"] = [
        {
            "id": "window_1",
            "starts_at": stamp(now),
            "ends_at": stamp(now + timedelta(hours=1)),
        }
    ]
    sources = {}
    previous = {
        "version": "buyer_state_v1",
        "inference_version": "observed_v1",
        "episode_id": c["origin_episode_id"],
        "as_of": stamp(now - timedelta(hours=26)),
        "snapshot_sha256": "d" * 64,
        "observations": [],
        "journey_stage": "cart",
        "observed_friction": [],
        "motivation_status": "unknown",
        "purchase_readiness": {
            "status": "unestimated",
            "probability": None,
            "target": "paid_order_within_7d_without_vakaru",
            "model_version": None,
        },
        "coverage": {
            "event_count": 0,
            "captured_signals": [],
            "missing_signals": ["calibrated_readiness"],
        },
    }
    current = copy.deepcopy(previous)
    current["as_of"] = stamp(now - timedelta(hours=1))
    current["observations"] = [
        {
            "event_id": "shipping-visit",
            "event_name": "page_viewed",
            "occurred_at": stamp(now - timedelta(hours=1)),
        }
    ]
    values = {
        "buyer": {
            "merchant_id": merchant,
            "buyer_state": current,
            "previous_buyer_state": previous,
            "delta_complete": True,
            "reported_statements": [],
        },
        "cart": {"merchant_id": merchant, "product_ids": ["shoe-1"], "market": "IN"},
        "shipping": {
            "merchant_id": merchant,
            "origin": "vakaru_generated_brand_evidence",
            "approved": True,
            "kind": "shipping_policy",
            "text": "Delivery within 5 business days in India.",
            "product_ids": ["shoe-1"],
            "market": "IN",
            "valid_until": stamp(now + timedelta(days=2)),
        },
    }
    for k, v in values.items():
        sources[k] = {"ref": {"id": k, "revision": 1, "sha256": digest(v)}, "value": v}
    c.update(
        buyer_state_ref=sources["buyer"]["ref"],
        cart_ref=sources["cart"]["ref"],
        fact_refs=[sources["shipping"]["ref"]],
    )
    c["fact_set_sha256"] = digest(c["fact_refs"])
    c["eligible_purpose_ids"] = ["shipping"]
    return {
        "schema_version": "adaptive_evaluation_v1",
        "context": c,
        "snapshot_sha256": "a" * 64,
        "message_id": str(uuid4()),
        "message_revision": 0,
        "ordinal": 2,
        "sources": sources,
        "spans": [
            {
                "id": "visit",
                "source_id": "buyer",
                "text": "page_viewed",
                "attribution": "behavior",
                "occurred_at": stamp(now - timedelta(hours=1)),
                "received_at": stamp(now - timedelta(hours=1)),
                "scope": ["shoe-1", "IN"],
            }
        ],
        "purposes": [
            {
                "id": "shipping",
                "kind": "shipping_policy",
                "description": "Explain the applicable shipping policy.",
                "fact_ids": ["shipping"],
                "evidence_ids": ["visit"],
                "concern": "Shipping information may help.",
                "reported_span_id": None,
            }
        ],
        "reply_channel_supported": False,
    }


def answer(request, changes=None):
    result = {"model": "typesafe/jev-1.13", "answers": {}, "usage": {"cost": 0.001}}
    for key, q in request["questions"].items():
        options = (
            list(q["criteria"])
            if q["type"] == "choice"
            else [str(i) for i in range(len(q["criteria"]))]
        )
        winner = {
            "TF01": "no_useful_change",
            "TF02": "behavior_only",
            "TF03": "3",
            "TF04": "adds_useful_information",
        }[key[:4]]
        a = {
            "type": q["type"],
            "confidence": 0.95,
            "probabilities": {x: int(x == winner) for x in options},
        }
        if q["type"] == "choice":
            a["choice"] = winner
        else:
            a.update(score=3, legend={str(i): v for i, v in enumerate(q["criteria"])})
        result["answers"][key] = a
    if changes:
        changes(result)
    return result
