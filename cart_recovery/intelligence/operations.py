"""Local, atomically replaced pre-launch controls. No network or shopper authority."""

import hashlib
import json
import math
import os
from pathlib import Path

NAMES = {
    "policy_extraction": "POLICY",
    "recovery_intelligence": "RECOVERY",
    "draft_claims": "CLAIMS",
}
FIELDS = {
    "version",
    "deployment_id",
    "environment",
    "provider",
    "model",
    "approved_models",
    "no_publish",
    "no_shopper_send",
    "daily_merchant_usd",
    "daily_environment_usd",
    "workflows",
}


def control(workflow, merchant):
    if not os.getenv("TYPESAFE_DEPLOYMENT_FILE") and os.getenv(
        "TYPESAFE_PRELAUNCH", ""
    ) in {"", "0"}:
        return "", None
    try:
        with Path(os.environ["TYPESAFE_DEPLOYMENT_FILE"]).open("rb") as stream:
            raw = stream.read(65537)
        d = json.loads(raw)
        if not (len(raw) <= 65536 and set(d) == FIELDS):
            raise ValueError("invalid_deployment")
        if not (d["version"] == "typesafe_deployment_v1"):
            raise ValueError("invalid_deployment")
        if not (
            isinstance(d["deployment_id"], str) and 0 < len(d["deployment_id"]) <= 100
        ):
            raise ValueError("invalid_deployment")
        if not (
            d["environment"] in {"development", "staging"}
            and d["environment"] == os.getenv("TYPESAFE_ENVIRONMENT")
        ):
            raise ValueError("invalid_deployment")
        if not (
            d["provider"] == "openrouter"
            and d["no_publish"] is True
            and d["no_shopper_send"] is True
        ):
            raise ValueError("invalid_deployment")
        if not (isinstance(d["model"], str) and d["model"]):
            raise ValueError("invalid_deployment")
        if not (
            isinstance(d["approved_models"], list)
            and d["approved_models"]
            and all(isinstance(m, str) and m for m in d["approved_models"])
        ):
            raise ValueError("invalid_deployment")
        for key, cap in [("daily_merchant_usd", 1), ("daily_environment_usd", 10)]:
            if not (
                type(d[key]) in {int, float}
                and math.isfinite(d[key])
                and 0 < d[key] <= cap
            ):
                raise ValueError("invalid_deployment")
        if not (isinstance(d["workflows"], dict) and set(d["workflows"]) == set(NAMES)):
            raise ValueError("invalid_deployment")
        for w in d["workflows"].values():
            if not (
                set(w) == {"mode", "merchants"}
                and w["mode"] in {"off", "observe", "assist", "enforce"}
            ):
                raise ValueError("invalid_deployment")
            if not (
                isinstance(w["merchants"], list)
                and all(isinstance(m, str) and m for m in w["merchants"])
            ):
                raise ValueError("invalid_deployment")
        w = d["workflows"][workflow]
        if not (
            w["mode"] != "off"
            and w["mode"] == os.getenv("TYPESAFE_" + NAMES[workflow] + "_MODE")
        ):
            raise ValueError("invalid_deployment")
        if not (merchant and merchant in w["merchants"]):
            raise ValueError("invalid_deployment")
        if not (
            merchant
            in {
                m.strip()
                for m in os.getenv(
                    "TYPESAFE_" + NAMES[workflow] + "_MERCHANTS", ""
                ).split(",")
            }
        ):
            raise ValueError("invalid_deployment")
    except (AssertionError, OSError, ValueError, TypeError, KeyError):
        raise ValueError("prelaunch_workflow_held") from None
    return hashlib.sha256(raw).hexdigest(), d
