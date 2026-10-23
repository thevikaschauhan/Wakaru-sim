"""Authenticated internal AF07 surface. No model client or database is opened when off."""

import hashlib
import hmac
import os
import threading
import time
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request
from jsonschema import ValidationError

from .journal import Conflict, Journal
from .planning import contracts
from .draft import POLICY, RUBRIC, DraftChecker
from cart_recovery.intelligence.typesafe import Client

PATH = "/api/internal/v2/recovery/check-draft"
bp = Blueprint("adaptive_draft_v2", __name__)
_lock = threading.Lock()


def signature(secret, timestamp, merchant, key, raw):
    prefix = f"{timestamp}\nPOST\n{PATH}\n{merchant}\n{key}\n".encode()
    return hmac.new(secret.encode(), prefix + raw, hashlib.sha256).hexdigest()


def planner():
    with _lock:
        if "adaptive_draft_checker" not in current_app.extensions:
            dsn = os.getenv("ADAPTIVE_SEMANTIC_DATABASE_URL", "")
            environment = os.getenv("ADAPTIVE_SEMANTIC_ENVIRONMENT", "local")
            if not dsn or (
                environment == "production" and not POLICY["production_qualified"]
            ):
                raise Conflict("semantic_unconfigured_or_unqualified")
            client = Client()
            current_app.extensions["adaptive_draft_checker"] = DraftChecker(
                Journal(
                    dsn, client, POLICY, environment, workflow="draft", rubric=RUBRIC
                )
            )
        return current_app.extensions["adaptive_draft_checker"]


@bp.post(PATH)
def evaluate():
    started = time.monotonic()
    if os.getenv("ADAPTIVE_FOLLOWUP_DRAFT_ENABLED") != "true":
        return jsonify(error="adaptive_draft_disabled"), 503
    api_key, secret = (
        os.getenv("WAKARU_API_KEY", ""),
        os.getenv("WAKARU_INTERNAL_SECRET", ""),
    )
    if (
        not api_key
        or not secret
        or not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key)
    ):
        return jsonify(error="unauthorized"), 401
    # Also bound chunked/unknown-length bodies before parsing or hashing them.
    raw = request.stream.read(128001)
    if len(raw) > 128000:
        return jsonify(error="input_overflow"), 413
    merchant, key = (
        request.headers.get("X-Merchant-Id", ""),
        request.headers.get("Idempotency-Key", ""),
    )
    ts = request.headers.get("X-Wakaru-Timestamp", "")
    try:
        if (
            str(UUID(merchant)) != merchant
            or not key
            or len(key) > 128
            or any(ord(c) < 33 or ord(c) > 126 for c in key)
        ):
            raise ValueError()
        if abs(time.time() - int(ts)) > 300:
            raise ValueError()
    except ValueError:
        return jsonify(error="unauthorized"), 401
    expected = signature(secret, ts, merchant, key, raw)
    if not hmac.compare_digest(request.headers.get("X-Wakaru-Signature", ""), expected):
        return jsonify(error="unauthorized"), 401
    try:
        body = contracts.loads(raw)
        result = planner().evaluate(body, merchant, key)
        response = jsonify(result)
        response.headers["X-AF07-Elapsed-Ms"] = str(
            int((time.monotonic() - started) * 1000)
        )
        return response
    except Conflict:
        return jsonify(error="semantic_conflict"), 409
    except (
        ValueError,
        TypeError,
        KeyError,
        ValidationError,
        OverflowError,
        AttributeError,
    ):
        return jsonify(error="invalid_evaluation"), 400
    except Exception:
        # No payloads, credentials, exception text or raw provider output in logs.
        current_app.logger.warning("adaptive semantic storage/transport unavailable")
        return jsonify(error="semantic_unavailable"), 503
