"""RR02 fresh Engine authority. No cached permits and no send authorization."""

import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .journal import Conflict
from .planning import digest

PATH = "/v1/recovery/release/authority"


def environment():
    value = os.getenv("ENVIRONMENT", "") or "local"
    if value not in {"local", "test", "staging", "production"}:
        raise Conflict("invalid_runtime_environment")
    semantic = os.getenv("ADAPTIVE_SEMANTIC_ENVIRONMENT", value)
    if semantic != value:
        raise Conflict("semantic_environment_mismatch")
    return value


def valid_environment():
    try:
        environment()
        return True
    except Conflict:
        return False


def required():
    env = environment()
    flag = os.getenv("RR02_ENFORCED", "")
    if flag not in {"", "true", "false"}:
        raise Conflict("invalid_enforcement_flag")
    return env in {"staging", "production"} or flag == "true"


@lru_cache(maxsize=1)
def artifact_hash():
    # The explicit runtime manifest is identical in the source checkout and
    # built image. Include HTTP/auth/config and split requirements, not just
    # cart_recovery or obsolete lockfiles from a different packaging layout.
    root = Path(__file__).resolve().parents[2]
    manifest_path = root / "ops/runtime/lean-files.txt"
    names = {"ops/runtime/lean-files.txt"}
    names.update(line for line in manifest_path.read_text().splitlines()
                 if line and not line.startswith("#"))
    manifest = {}
    for name in sorted(names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise Conflict("invalid_runtime_manifest")
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise Conflict("runtime_artifact_missing")
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(manifest)


def permit(merchant, journal):
    if not required():
        return None
    if os.getenv("RR02_PROCESS") != "wakaru-semantic":
        raise Conflict("invalid_process_role")
    env = environment()
    body = {
        "release_sha256": os.getenv("RR02_RELEASE_SHA256", ""),
        "environment": env,
        "process": "wakaru-semantic",
        "artifact_sha256": artifact_hash(),
        "action": "plan" if journal.workflow == "planning" else "compose",
        "binding": {
            "deployment_sha256": journal.deployment,
            "threshold_sha256": digest(journal.policy),
        },
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    key = hashlib.sha256(raw).hexdigest()
    secret = os.getenv("RR02_AUTHORITY_SECRET", "")
    base = os.getenv("RR02_AUTHORITY_URL", "").rstrip("/")
    u = urlsplit(base)
    if (
        not secret
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path
        or not (
            u.scheme == "https"
            or (
                u.scheme == "http"
                and env in {"local", "test"}
                and u.hostname in {"127.0.0.1", "::1"}
            )
        )
    ):
        raise Conflict("invalid_authority_endpoint")
    ts = str(int(time.time()))
    prefix = f"{ts}\nPOST\n{PATH}\n{merchant}\n{key}\n".encode()
    headers = {
        "Content-Type": "application/json",
        "X-Merchant-Id": merchant,
        "Idempotency-Key": key,
        "X-AF07-Timestamp": ts,
        "X-AF07-Signature": hmac.new(
            secret.encode(), prefix + raw, hashlib.sha256
        ).hexdigest(),
    }
    try:
        with (
            httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "POST", base + PATH, content=raw, headers=headers
            ) as response,
        ):
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > 4096:
                    raise ValueError()
            result = json.loads(data)
            if (
                response.status_code != 200
                or set(result)
                != {
                    "release_sha256",
                    "global_revision",
                    "tenant_revision",
                    "request_sha256",
                    "valid_until",
                }
                or result["request_sha256"] != key
                or result["release_sha256"] != body["release_sha256"]
                or not time.time() < result["valid_until"] <= time.time() + 5
            ):
                raise ValueError()
            return (
                result["release_sha256"],
                result["global_revision"],
                result["tenant_revision"],
                key,
            )
    except Exception as exc:
        raise Conflict("promotion_authority_unavailable") from exc


def evaluate(planner, body, merchant, key):
    before = permit(merchant, planner.journal)
    result = planner.evaluate(body, merchant, key)
    if permit(merchant, planner.journal) != before:
        raise Conflict("promotion_authority_changed")
    return result


class MaintenanceJournal:
    """Privacy/accounting remain usable without loading a model or release grant."""

    def __init__(self):
        self.environment = environment()
        self.dsn = os.getenv("ADAPTIVE_SEMANTIC_MAINTENANCE_DATABASE_URL") or os.getenv(
            "ADAPTIVE_SEMANTIC_DATABASE_URL", ""
        )
        if not self.dsn:
            raise Conflict("semantic_storage_unconfigured")

    @contextmanager
    def transaction(self):
        import psycopg

        with psycopg.connect(self.dsn, connect_timeout=1) as db:
            db.execute("SET LOCAL lock_timeout='1s'")
            db.execute("SET LOCAL statement_timeout='3s'")
            yield db
