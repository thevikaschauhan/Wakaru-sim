"""Explicit offline C16 service factory. No startup hook, key discovery or listener."""

from contextlib import closing
from dataclasses import dataclass
import json
import time

from flask import Flask, jsonify, request
from integration_lineage import KINDS, Lineage, digest, fingerprint, resource_key, token
from integration_service_auth import MAX_BODY, ServiceGrants

PREFIX = "/internal/integrations/lineage/"
CAPTURE = PREFIX + "capture"
READ = PREFIX + "read"
ADMIT = PREFIX + "admit"


@dataclass(frozen=True)
class Binding:
    """Reviewed evidence for a native request_key or plan revision."""

    alias_kind: str
    alias_ref: str
    subject: str
    alias_proof: str
    sources: tuple
    proof: str

    def __post_init__(self):
        if self.alias_kind not in KINDS or not isinstance(self.sources, tuple):
            raise ValueError("invalid_binding")
        if (
            not 1 <= len(self.sources) <= 64
            or tuple(sorted(set(self.sources))) != self.sources
        ):
            raise ValueError("invalid_sources")
        for value in (
            self.alias_ref,
            self.subject,
            self.alias_proof,
            self.proof,
            *self.sources,
        ):
            token(value)


def unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate_key")
    return value


def sqlite_writer_trigger(kind, operation):
    table = {"raw_output": "raw_outputs", "plan": "recovery_plans"}[kind]
    name = f"c16_authenticated_{table}_{operation.lower()}"
    sql = (
        f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
        "WHEN c16_authenticated_writer() IS NOT 1 "
        "BEGIN SELECT RAISE(ABORT,'authenticated_writer_required'); END"
    )
    return name, sql


def enroll_sqlite_file(op, kinds):
    """Explicit file-wide enrollment for each listed legacy table. No implicit DDL."""
    if not op.sqlite or not kinds:
        raise ValueError("dedicated_sqlite_file_required")
    with op.tx():
        for kind in kinds:
            op.guard(kind)
            for operation in ("INSERT", "UPDATE"):
                name, sql = sqlite_writer_trigger(kind, operation)
                existing = op.q(
                    "SELECT sql FROM main.sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                if existing is None:
                    op.q(sql)
                elif existing[0] != sql:
                    raise ValueError("unexpected_writer_guard")


def check_adapter(op, kind):
    op.guard(kind)
    if op.sqlite:
        for operation in ("INSERT", "UPDATE"):
            name, sql = sqlite_writer_trigger(kind, operation)
            row = op.q(
                "SELECT sql FROM main.sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()
            if row is None or row[0] != sql:
                raise ValueError("authenticated_writer_guard_required")


def reviewed_binding(op, bindings, kind, revision):
    binding = bindings.get((op.m, op.s, kind, revision))
    if binding is None:
        raise ValueError("reviewed_binding_required")
    alias = op.q(
        "SELECT subject,proof FROM integration_lineage_aliases WHERE merchant=? AND store=? AND kind=? AND ref=?",
        (op.m, op.s, binding.alias_kind, binding.alias_ref),
    ).fetchone()
    if (
        not alias
        or tuple(alias) != (binding.subject, binding.alias_proof)
        or op.stopped(binding.subject, binding.sources)
    ):
        raise ValueError("lineage_unavailable")
    return binding


def binding_digest(op, kind, revision, binding):
    return digest([
        "integration_lineage_admission_v1", op.service, op.m, op.s, kind, revision,
        binding.alias_kind, binding.alias_ref, binding.subject, binding.alias_proof,
        binding.sources, binding.proof,
    ])


def access(op, bindings, kind, key, body, *, admitted=None, selected_revision=None):
    """None requests a read. Only an approved, never-written plan can miss."""
    with op.tx():
        check_adapter(op, kind)
        if op.sqlite and body is not None:
            op.db.create_function("c16_authenticated_writer", 0, lambda: 1)
        revision = key
        if kind == "raw_output":
            parent = op.q(
                "SELECT request_key FROM main.attempts WHERE merchant=? AND id=?"
                if op.sqlite
                else "SELECT request_key FROM intelligence.attempts WHERE merchant_id=? AND id=? FOR UPDATE",
                (op.m, key),
            ).fetchone()
            if not parent:
                raise ValueError("owned_attempt_required")
            revision = parent[0]
            if selected_revision is not None and revision != selected_revision:
                raise ValueError("selected_attempt_mismatch")
        binding = reviewed_binding(op, bindings, kind, revision)
        current = binding_digest(op, kind, revision, binding)
        if admitted is not None and current != admitted:
            raise ValueError("binding_changed")
        registered = op.q(
            "SELECT store,lineage FROM integration_lineage_resources WHERE merchant=? AND kind=? AND key=?",
            (op.m, kind, key),
        ).fetchone()
        if registered:
            if registered[0] != op.s:
                raise ValueError("resource_conflict")
            existing = op._read_locked(kind, key)
            expected = fingerprint(
                op.s,
                kind,
                key,
                binding.subject,
                binding.proof,
                digest(existing),
                binding.alias_kind,
                binding.alias_ref,
                binding.alias_proof,
                list(binding.sources),
            )
            if expected != registered[1] or (body is not None and body != existing):
                raise ValueError("resource_conflict")
            if op.sqlite:
                table, column = (
                    ("raw_outputs", "id")
                    if kind == "raw_output"
                    else ("recovery_plans", "revision")
                )
                fresh = op.q(
                    f"SELECT 1 FROM main.{table} WHERE merchant=? AND {column}=? AND created>=?",
                    (op.m, key, time.time() - 7 * 86400),
                ).fetchone()
            else:
                fresh = op.q(
                    "SELECT 1 FROM intelligence.raw_outputs WHERE merchant_id=? AND attempt_id=? AND created_at>=now()-interval '7 days'",
                    (op.m, key),
                ).fetchone()
            if not fresh:
                raise ValueError("expired_payload")
            return existing, current
        if op.payload(kind, key, lock=True) is not None:
            raise ValueError("unregistered_legacy_payload")
        if body is None:
            if kind != "plan":
                raise ValueError("registered_record_required")
            return None, current
        if kind == "plan":
            op.q(
                "INSERT INTO main.recovery_plans(merchant,revision,payload,created) VALUES(?,?,?,?)",
                (op.m, key, body, time.time()),
            )
        elif op.sqlite:
            op.q(
                "INSERT INTO main.raw_outputs(merchant,id,body,created) VALUES(?,?,?,?)",
                (op.m, key, body, time.time()),
            )
        else:
            op.q(
                "INSERT INTO intelligence.raw_outputs(merchant_id,attempt_id,body) VALUES(?,?,?)",
                (op.m, key, body),
            )
        op._adopt_locked(
            kind,
            key,
            binding.alias_kind,
            binding.alias_ref,
            list(binding.sources),
            binding.proof,
        )
        return None, current


def create_lineage_app(
    service, *, enabled=False, grants=(), bindings=None, connect=None
):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    if enabled is not True:
        return app
    if (
        service not in {"engine", "inkwell", "wakaru"}
        or not callable(connect)
        or not bindings
    ):
        raise ValueError("explicit_lineage_configuration_required")
    verifier = ServiceGrants(service, grants)
    scopes = frozenset(
        (g["merchant_id"], g["store_id"]) for g in verifier.grants.values()
    )
    # Native rows have merchant and request key, but no store column. Reject
    # ambiguous first-writer authority before either store can capture the row.
    allowed_operations = {"POST " + path for path in (ADMIT, CAPTURE, READ)}
    credentials = list(verifier.grants.values())
    if len({g["secret"] for g in credentials}) != len(credentials) or any(
        set(g["operations"]) - allowed_operations for g in credentials
    ):
        raise ValueError("lineage_grant_separation_required")
    bindings = dict(bindings)
    if len({(m, k, revision) for m, _, k, revision in bindings}) != len(bindings):
        raise ValueError("ambiguous_native_store_binding")
    for (merchant, store, kind, revision), binding in bindings.items():
        if (
            (merchant, store) not in scopes
            or not isinstance(binding, Binding)
            or kind
            not in ({"raw_output", "plan"} if service == "wakaru" else {"raw_output"})
        ):
            raise ValueError("invalid_enrollment")
        resource_key(revision)

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(ADMIT)
    @app.post(CAPTURE)
    @app.post(READ)
    def handle():
        raw = request.get_data()
        principal = verifier.verify(
            request.headers.get("Authorization", ""), "POST", request.path, raw
        )
        target = request.environ.get("RAW_URI", request.path)
        if not principal or request.query_string or target != request.path:
            return jsonify(error="unauthorized"), 401
        capture = request.path == CAPTURE
        try:
            data = json.loads(raw, object_pairs_hook=unique_object)
            fields = {"merchant_id", "kind", "key"}
            if capture:
                fields.update(("body", "binding"))
            elif request.path == ADMIT and isinstance(data, dict):
                fields.update({"attempt_id", "binding"}.intersection(data))
            if (
                not isinstance(data, dict)
                or set(data) != fields
                or data["merchant_id"] != principal["merchant_id"]
            ):
                raise ValueError()
            kind, key = data["kind"], resource_key(data["key"])
            admitted = token(data["binding"]) if "binding" in data else None
            attempt = resource_key(data["attempt_id"]) if "attempt_id" in data else None
            if kind not in ("raw_output", "plan") or (
                capture and not isinstance(data["body"], str)
            ):
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            return jsonify(error="invalid_request"), 400
        try:
            # Connection factory chooses the enrolled database, never a request path/DSN.
            with closing(
                connect(principal["merchant_id"], principal["store_id"])
            ) as db:
                op = Lineage(
                    db,
                    service,
                    principal["merchant_id"],
                    principal["store_id"],
                    enabled=True,
                    approved_scopes=scopes,
                )
                if request.path == ADMIT:
                    if kind != "raw_output":
                        raise ValueError("unsupported_admission")
                    if attempt is not None:
                        # Validate the actual winner's retained membership. An updated
                        # enrollment alone cannot authorize a historical selection.
                        access(op, bindings, kind, attempt, None,
                               admitted=admitted, selected_revision=key)
                        return jsonify(admitted=True)
                    with op.tx():
                        check_adapter(op, kind)
                        binding = reviewed_binding(op, bindings, kind, key)
                        current = binding_digest(op, kind, key, binding)
                        if admitted is not None and current != admitted:
                            raise ValueError("binding_changed")
                    return jsonify(admitted=True, binding=current)
                result, current = access(op, bindings, kind, key, data.get("body"),
                                         admitted=admitted)
            if capture:
                return jsonify(stored=True)
            if kind == "plan":
                return jsonify(body=result, binding=current)
            return jsonify(body=result)
        except ValueError:
            return jsonify(error="lineage_unavailable"), 409
        except Exception:
            # Database errors may contain raw provider text. Never echo or log them.
            return jsonify(error="lineage_unavailable"), 503

    return app
