"""Shared v2 journal: leases, first-result selection and pre-I/O cost reservations.

All scopes use the same authority, including future Inkwell draft reservations.
Unknown usage retains its reservation. A crash can duplicate external billing;
all attempts are charged, and a stale worker cannot select a response.
"""

import json
import math
import uuid
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING

from .planning import digest, strict_response, RUBRIC, contracts


def cached(selected, key):
    return {
        **selected,
        "cache_hit": True,
        "request_key": key,
        "accounting": {**selected.get("accounting", {}), "new_attempts": 0},
    }


class Conflict(ValueError):
    pass


class Journal:
    def __init__(
        self,
        dsn,
        client,
        policy,
        environment="local",
        connect=None,
        workflow="planning",
        rubric=RUBRIC,
    ):
        self.dsn, self.client, self.policy, self.environment = (
            dsn,
            client,
            policy,
            environment,
        )
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self.connect = connect
        self.workflow = workflow
        if client.model != policy["model"] or client.model not in client.approved:
            raise Conflict("semantic_unapproved_model")
        self.client_binding = client.binding
        self.deployment = digest(
            {
                "client": client.binding,
                "rubric": rubric,
                "budgets": {
                    k: policy[k]
                    for k in ("limits_micros", "reservation_micros", "attempt_limit")
                },
            }
        )

    @contextmanager
    def transaction(self):
        with self.connect(self.dsn, connect_timeout=1) as db:
            db.execute("SET LOCAL lock_timeout='1s'")
            db.execute("SET LOCAL statement_timeout='3s'")
            yield db

    def check_deployment(self, db):
        if self.client.binding != self.client_binding:
            raise Conflict("semantic_client_binding_changed")
        row = db.execute(
            "SELECT d.binding,d.enabled,d.revision,d.threshold_sha256,r.policy,b.limits FROM af6_deployments d JOIN af6_threshold_registry r ON r.sha256=d.threshold_sha256 JOIN af6_budget_policies b ON b.environment=d.environment WHERE d.environment=%s AND workflow=%s FOR SHARE OF d,r,b",
            (self.environment, self.workflow),
        ).fetchone()
        if (
            not row
            or not row[1]
            or row[0] != self.deployment
            or row[3] != digest(self.policy)
            or row[4] != self.policy
            or row[5] != self.policy["limits_micros"]
        ):
            raise Conflict("semantic_deployment_disabled_or_changed")
        return row[2]

    def revision(self):
        with self.transaction() as db:
            return self.check_deployment(db)

    def evaluation(
        self, merchant, key, request_hash, result=None, revision=None, journey=None
    ):
        with self.transaction() as db:
            if self.check_deployment(db) != revision:
                raise Conflict("semantic_stale_deployment")
            db.execute(
                "INSERT INTO af6_evaluations(environment,merchant,idempotency_key,request_hash,journey_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (self.environment, merchant, key, request_hash, journey),
            )
            row = db.execute(
                "SELECT request_hash,result,journey_id::text FROM af6_evaluations WHERE environment=%s AND merchant=%s AND idempotency_key=%s FOR UPDATE",
                (self.environment, merchant, key),
            ).fetchone()
            if row[0] != request_hash or row[2] != journey:
                raise Conflict("IDEMPOTENCY_CONFLICT")
            if row[1] is not None:
                return row[1]
            if result is not None:
                db.execute(
                    "UPDATE af6_evaluations SET result=%s::jsonb WHERE environment=%s AND merchant=%s AND idempotency_key=%s",
                    (json.dumps(result), self.environment, merchant, key),
                )
            return result

    def judge(self, identity, request):
        if identity["phase"] != self.workflow:
            raise Conflict("semantic_workflow_mismatch")
        started = time.monotonic()
        malformed = []
        overrun = []
        denials = []
        key = digest(
            {"identity": identity, "request": request, "deployment": self.deployment}
        )
        merchant = identity["merchant_id"]
        token = str(uuid.uuid4())
        with self.transaction() as db:
            revision = self.check_deployment(db)
            db.execute(
                "INSERT INTO af6_requests(environment,merchant,request_key,request_hash,request,journey_id) VALUES(%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING",
                (
                    self.environment,
                    merchant,
                    key,
                    digest(request),
                    json.dumps(request),
                    identity["journey_id"],
                ),
            )
            row = db.execute(
                "SELECT selected,lease_until>clock_timestamp(),attempts,retry_at>clock_timestamp() FROM af6_requests WHERE environment=%s AND merchant=%s AND request_key=%s FOR UPDATE",
                (self.environment, merchant, key),
            ).fetchone()
            if not row:
                raise Conflict("semantic_erased_or_stale")
            if row[0] is not None:
                return cached(row[0], key)
            if row[1] or row[3]:
                return {
                    "status": "unavailable",
                    "error_class": "semantic_backpressure",
                    "request_key": key,
                }
            if row[2] >= self.policy["attempt_limit"]:
                return {
                    "status": "unavailable",
                    "error_class": "semantic_attempts_exhausted",
                    "request_key": key,
                }
            circuit = db.execute(
                "SELECT open_until>clock_timestamp(),open_until IS NOT NULL,probe_until>clock_timestamp() FROM af6_circuits WHERE environment=%s AND merchant=%s FOR UPDATE",
                (self.environment, merchant),
            ).fetchone()
            if circuit and (circuit[0] or circuit[2]):
                return {
                    "status": "unavailable",
                    "error_class": "semantic_circuit_open",
                    "request_key": key,
                }
            if circuit and circuit[1]:
                db.execute(
                    "UPDATE af6_circuits SET probe_token=%s,probe_until=clock_timestamp()+interval '10 seconds' WHERE environment=%s AND merchant=%s",
                    (token, self.environment, merchant),
                )
            db.execute(
                "UPDATE af6_requests SET lease_token=%s,lease_until=clock_timestamp()+interval '10 seconds' WHERE environment=%s AND merchant=%s AND request_key=%s",
                (token, self.environment, merchant, key),
            )

        def reserve(attempt):
            if time.monotonic() - started >= self.policy["deadline_ms"] / 1000:
                raise Conflict("semantic_deadline")
            with self.transaction() as db:
                if self.check_deployment(db) != revision:
                    raise Conflict("semantic_stale_deployment")
                row = db.execute(
                    "SELECT attempts FROM af6_requests WHERE environment=%s AND merchant=%s AND request_key=%s AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE",
                    (self.environment, merchant, key, token),
                ).fetchone()
                if not row or row[0] >= self.policy["attempt_limit"]:
                    denials.append("semantic_attempts_exhausted")
                    raise Conflict("semantic_attempts_exhausted")
                day = db.execute(
                    "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD')"
                ).fetchone()[0]
                scopes = {
                    "batch": (key, "lifetime"),
                    "message": (
                        merchant
                        + ":"
                        + identity["message_id"]
                        + ":"
                        + str(identity["message_revision"]),
                        "lifetime",
                    ),
                    "journey": (merchant + ":" + identity["journey_id"], "lifetime"),
                    "merchant": (merchant, day),
                    "environment": ("shared", day),
                }
                amount = self.policy["reservation_micros"]
                # Identical sorted lock order across every service/worker.
                for scope, (ident, period) in sorted(scopes.items()):
                    db.execute(
                        "INSERT INTO af6_budgets(environment,scope,identity,period) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (self.environment, scope, ident, period),
                    )
                    used = db.execute(
                        "SELECT spent FROM af6_budgets WHERE environment=%s AND scope=%s AND identity=%s AND period=%s FOR UPDATE",
                        (self.environment, scope, ident, period),
                    ).fetchone()[0]
                    if used + amount > self.policy["limits_micros"][scope]:
                        denials.append("semantic_budget_exhausted")
                        raise Conflict("semantic_budget_exhausted")
                db.execute(
                    "INSERT INTO af6_attempts(id,environment,merchant,request_key,lease_token,reserved,charged) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (attempt, self.environment, merchant, key, token, amount, amount),
                )
                for scope, (ident, period) in sorted(scopes.items()):
                    db.execute(
                        "UPDATE af6_budgets SET spent=spent+%s WHERE environment=%s AND scope=%s AND identity=%s AND period=%s",
                        (amount, self.environment, scope, ident, period),
                    )
                    db.execute(
                        "INSERT INTO af6_allocations VALUES(%s,%s,%s,%s,%s)",
                        (attempt, self.environment, scope, ident, period),
                    )
                db.execute(
                    "UPDATE af6_requests SET attempts=attempts+1 WHERE environment=%s AND merchant=%s AND request_key=%s",
                    (self.environment, merchant, key),
                )

        def settle(attempt):
            if attempt.get("http_status") == 200:
                try:
                    value = contracts.loads(attempt.get("_raw_response", ""))
                    strict_response(request, value, [self.client.model], self.policy)
                except Exception:
                    malformed.append(True)
            cost = attempt.get("cost")
            measured = (
                isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(cost)
                and cost >= 0
            )
            with self.transaction() as db:
                row = db.execute(
                    "SELECT charged,finished_at FROM af6_attempts WHERE id=%s AND lease_token=%s FOR UPDATE",
                    (attempt["id"], token),
                ).fetchone()
                if not row or row[1] is not None:
                    return
                charged = (
                    int(
                        (Decimal(str(cost)) * 1000000).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    )
                    if measured
                    else row[0]
                )
                for env, scope, ident, period in db.execute(
                    "SELECT environment,scope,identity,period FROM af6_allocations WHERE attempt=%s ORDER BY scope,identity,period",
                    (attempt["id"],),
                ).fetchall():
                    db.execute(
                        "UPDATE af6_budgets SET spent=spent+%s WHERE environment=%s AND scope=%s AND identity=%s AND period=%s",
                        (charged - row[0], env, scope, ident, period),
                    )
                safe = {k: v for k, v in attempt.items() if k != "_raw_response"}
                db.execute(
                    "UPDATE af6_attempts SET charged=%s,result=%s::jsonb,raw_sha256=%s,finished_at=clock_timestamp() WHERE id=%s",
                    (
                        charged,
                        json.dumps(safe),
                        digest(attempt.get("_raw_response", "")),
                        attempt["id"],
                    ),
                )
                if charged > self.policy["reservation_micros"]:
                    overrun.append(True)
                    db.execute(
                        "INSERT INTO af6_circuits(environment,merchant,failures,open_until) VALUES(%s,%s,3,'infinity') ON CONFLICT(environment,merchant) DO UPDATE SET open_until='infinity'",
                        (self.environment, merchant),
                    )

        result = self.client.evaluate(request, before=reserve, finish=settle)
        if denials:
            result = {**result, "status": "unavailable", "error_class": denials[-1]}
        if malformed or overrun:
            result = {
                **result,
                "status": "unresolved",
                "error_class": "semantic_invalid_result"
                if malformed
                else "semantic_cost_overrun",
            }
        if time.monotonic() - started > self.policy["deadline_ms"] / 1000:
            result = {
                **result,
                "status": "unavailable",
                "error_class": "semantic_deadline",
            }
        if result["status"] == "evaluated":
            try:
                strict_response(
                    request, result["response"], [self.client.model], self.policy
                )
            except (ValueError, KeyError, TypeError):
                result = {
                    **result,
                    "status": "unresolved",
                    "error_class": "semantic_invalid_result",
                }
        result = {**result, "request_key": key, "cache_hit": False}
        with self.transaction() as db:
            if self.check_deployment(db) != revision:
                raise Conflict("semantic_stale_deployment")
            row = db.execute(
                "SELECT selected,lease_token::text,lease_until>clock_timestamp() FROM af6_requests WHERE environment=%s AND merchant=%s AND request_key=%s FOR UPDATE",
                (self.environment, merchant, key),
            ).fetchone()
            if not row:
                raise Conflict("semantic_erased_or_stale")
            if row[0] is not None:
                return cached(row[0], key)
            if row[1] != token or not row[2]:
                raise Conflict("semantic_stale_lease")
            db.execute(
                "UPDATE af6_circuits SET probe_token=NULL,probe_until=NULL WHERE environment=%s AND merchant=%s AND probe_token=%s",
                (self.environment, merchant, token),
            )
            accounting = db.execute(
                "SELECT count(*),coalesce(sum(charged),0),count(*) FILTER(WHERE finished_at IS NULL OR result->>'cost' IS NULL) FROM af6_attempts WHERE environment=%s AND merchant=%s AND request_key=%s",
                (self.environment, merchant, key),
            ).fetchone()
            result["accounting"] = {
                "total_attempts": accounting[0],
                "charged_micros": int(accounting[1]),
                "unknown_usage_attempts": accounting[2],
                "new_attempts": len(result.get("attempts", [])),
            }
            # Low confidence and schema failure are terminal selections, not retry targets.
            selected = (
                json.dumps(result)
                if result["status"] in {"evaluated", "unresolved"}
                else None
            )
            db.execute(
                "UPDATE af6_requests SET selected=%s::jsonb,lease_token=NULL,lease_until=NULL,retry_at=clock_timestamp()+interval '5 minutes' WHERE environment=%s AND merchant=%s AND request_key=%s",
                (selected, self.environment, merchant, key),
            )
            if result["status"] == "unavailable":
                db.execute(
                    "INSERT INTO af6_circuits(environment,merchant,failures) VALUES(%s,%s,1) ON CONFLICT(environment,merchant) DO UPDATE SET failures=af6_circuits.failures+1,open_until=CASE WHEN af6_circuits.failures>=1 THEN clock_timestamp()+interval '5 minutes' ELSE af6_circuits.open_until END",
                    (self.environment, merchant),
                )
            else:
                db.execute(
                    "UPDATE af6_circuits SET failures=0,open_until=NULL WHERE environment=%s AND merchant=%s AND open_until IS DISTINCT FROM 'infinity'::timestamptz",
                    (self.environment, merchant),
                )
        return result
