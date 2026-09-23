"""Read-only serving from a pinned registry revision and frozen observations."""

import os
import re

from .data import OBSERVED, SEMANTIC, TARGET, moment, vector
from .registry import Registry


def pinned_model(merchant):
    allowed = {
        v.strip()
        for v in os.getenv("CONVERSION_MODEL_MERCHANTS", "").split(",")
        if v.strip()
    }
    model_id = os.getenv("CONVERSION_MODEL_ID", "")
    return (
        model_id
        if merchant in allowed and re.fullmatch(r"[a-f0-9]{64}", model_id)
        else None
    )


def snapshot_features(state, intelligence):
    if state.get("version") != "buyer_state_v1" or state.get("journey_stage") not in {
        "cart",
        "checkout",
    }:
        raise ValueError("unsupported_snapshot")
    at = moment(state["as_of"])
    observations = state["observations"]
    if any(moment(o["occurred_at"]) > at for o in observations):
        raise ValueError("future_feature")
    observed = {k: 0 for k in OBSERVED}
    observed.update(
        event_count=len(observations),
        cart=int(state["journey_stage"] == "cart"),
        checkout=int(state["journey_stage"] == "checkout"),
    )
    alerts = {o.get("alert_type") for o in observations}
    observed.update(
        payment_error=int("PAYMENT_ERROR" in alerts),
        input_error=int(bool(alerts & {"INPUT_INVALID", "INPUT_REQUIRED"})),
        inventory_error=int(bool(alerts & {"INVENTORY_ERROR", "MERCHANDISE_ERROR"})),
    )
    available = intelligence.get("status") == "evaluated" and set(
        intelligence.get("model_features", {})
    ) == set(SEMANTIC)
    if available and (
        intelligence["based_on_snapshot_sha256"] != state["snapshot_sha256"]
        or moment(intelligence["as_of"]) < at
    ):
        raise ValueError("unbound_semantic_features")
    features = {
        "version": "recovery_features_v1",
        "observed": observed,
        "semantic": intelligence["model_features"]
        if available
        else {k: 0 for k in SEMANTIC},
        "semantic_available": available,
    }
    vector(features)
    return features


def estimate_snapshot(model_id, state, intelligence):
    result = {
        "version": "conversion_estimate_v1",
        "status": "not_available",
        "probability": None,
        "target": TARGET,
        "horizon_seconds": 604800,
        "model_version": model_id,
    }
    path = os.getenv("CONVERSION_REGISTRY_PATH", "")
    if not model_id or not os.path.isabs(path) or not os.path.isfile(path):
        return result
    try:
        features = snapshot_features(state, intelligence)
    except (ValueError, KeyError, TypeError):
        return {**result, "status": "out_of_distribution"}
    try:
        return Registry.readonly(path).estimate(
            model_id, features, intelligence["as_of"]
        )
    except Exception:
        return result
