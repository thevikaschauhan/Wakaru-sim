"""Authenticated internal AF06 surface. No model client or database is opened when off."""

import hashlib
import hmac
import os
import threading
import time
from uuid import UUID

from flask import Blueprint, current_app, jsonify, request
from jsonschema import ValidationError

from .journal import Conflict, Journal
from .planning import POLICY, Planner, contracts, digest
from cart_recovery.intelligence.typesafe import Client

PATH = "/api/internal/v2/recovery/evaluate"
bp = Blueprint("adaptive_followup_v2", __name__)
_lock = threading.Lock()


def signature(secret, timestamp, merchant, key, raw):
    prefix = f"{timestamp}\nPOST\n{PATH}\n{merchant}\n{key}\n".encode()
    return hmac.new(secret.encode(), prefix + raw, hashlib.sha256).hexdigest()


def planner():
    with _lock:
        if "adaptive_planner" not in current_app.extensions:
            dsn = os.getenv("ADAPTIVE_SEMANTIC_DATABASE_URL", "")
            environment = os.getenv("ADAPTIVE_SEMANTIC_ENVIRONMENT", "local")
            if not dsn or (
                environment == "production" and not POLICY["production_qualified"]
            ):
                raise Conflict("semantic_unconfigured_or_unqualified")
            client = Client()
            current_app.extensions["adaptive_planner"] = Planner(
                Journal(dsn, client, POLICY, environment)
            )
        return current_app.extensions["adaptive_planner"]


@bp.post(PATH)
def evaluate():
    started = time.monotonic()
    if os.getenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED") != "true":
        return jsonify(error="adaptive_planning_disabled"), 503
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
        response.headers["X-AF06-Elapsed-Ms"] = str(
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


@bp.post("/api/internal/v2/recovery/measurement")
def measure():
    # Independent read-only endpoint; importing this module does not open a model client.
    if os.getenv("AF10_MEASUREMENT_ENABLED") != "true" or os.getenv("ENVIRONMENT") == "production":
        return jsonify(error="measurement_disabled"), 503
    path = "/api/internal/v2/recovery/measurement"
    raw = request.stream.read(8193)
    merchant, key, ts = (request.headers.get(k, "") for k in ("X-Merchant-Id", "Idempotency-Key", "X-Wakaru-Timestamp"))
    secret, api_key = os.getenv("WAKARU_INTERNAL_SECRET", ""), os.getenv("WAKARU_API_KEY", "")
    try:
        if not secret or not api_key or not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key) or str(UUID(merchant)) != merchant or abs(time.time()-int(ts)) > 300:
            raise ValueError()
        prefix=f"{ts}\nPOST\n{path}\n{merchant}\n{key}\n".encode()
        if not hmac.compare_digest(request.headers.get("X-Wakaru-Signature", ""), hmac.new(secret.encode(),prefix+raw,hashlib.sha256).hexdigest()):
            raise ValueError()
    except ValueError:
        return jsonify(error="unauthorized"),401
    try:
        from .planning import digest
        from .measurement import measurement
        body=contracts.loads(raw)
        if len(raw)>8192 or set(body)!={"journey_id"} or str(UUID(body["journey_id"]))!=body["journey_id"] or key!=digest(body):
            raise ValueError()
        # Reuse the configured journal; this read never calls Client.evaluate.
        return jsonify(measurement(planner().journal,merchant,body["journey_id"]))
    except (ValueError,TypeError,KeyError):
        return jsonify(error="invalid_measurement"),422
    except Exception:
        return jsonify(error="measurement_unavailable"),503


@bp.post("/api/internal/v2/recovery/erase")
def erase():
    if (
        os.getenv("AF11_ERASURE_ENABLED") != "true"
        or os.getenv("ENVIRONMENT") == "production"
    ):
        return jsonify(error="erasure_disabled"), 503
    path = "/api/internal/v2/recovery/erase"
    raw = request.stream.read(8193)
    merchant, key, ts = (
        request.headers.get(k, "")
        for k in ("X-Merchant-Id", "Idempotency-Key", "X-Wakaru-Timestamp")
    )
    secret, api_key = (
        os.getenv("WAKARU_INTERNAL_SECRET", ""),
        os.getenv("WAKARU_API_KEY", ""),
    )
    try:
        if (
            not secret
            or not api_key
            or not hmac.compare_digest(request.headers.get("X-API-Key", ""), api_key)
            or str(UUID(merchant)) != merchant
            or abs(time.time() - int(ts)) > 300
        ):
            raise ValueError()
        prefix = f"{ts}\nPOST\n{path}\n{merchant}\n{key}\n".encode()
        if not hmac.compare_digest(
            request.headers.get("X-Wakaru-Signature", ""),
            hmac.new(secret.encode(), prefix + raw, hashlib.sha256).hexdigest(),
        ):
            raise ValueError()
    except ValueError:
        return jsonify(error="unauthorized"), 401
    try:
        from .erasure import erase_journey

        body = contracts.loads(raw)
        if (
            len(raw) > 8192
            or set(body) != {"journey_id"}
            or str(UUID(body["journey_id"])) != body["journey_id"]
            or key != digest(body)
        ):
            raise ValueError()
        return jsonify(erase_journey(planner().journal, merchant, body["journey_id"]))
    except (ValueError, TypeError, KeyError):
        return jsonify(error="invalid_erasure"), 422
    except Exception:
        return jsonify(error="erasure_unavailable"), 503
