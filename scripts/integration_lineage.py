"""C15 offline operator library. Never imported by application startup.

Callers must supply a dedicated connection and an explicitly reviewed scope.
No HTTP, environment discovery, provider calls, credential loading or schedulers.
The receipt covers registered rows only, never a whole customer or application.
"""

from contextlib import contextmanager
from itertools import groupby
import hashlib
import json
import re
import sqlite3
import uuid

VERSION = "integration_lineage_c15_v1"
KINDS = {"shopify_customer", "gorgias_customer", "anonymous_id", "cart_id"}
SERVICES = {"engine", "inkwell", "wakaru"}
TABLES = ("aliases", "resources", "sources", "stops", "requests", "members")
DDL = (
    "aliases(merchant TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,ref TEXT NOT NULL,subject TEXT NOT NULL,proof TEXT NOT NULL,PRIMARY KEY(merchant,store,kind,ref))",
    "resources(merchant TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,subject TEXT NOT NULL,proof TEXT NOT NULL,image TEXT NOT NULL,alias_kind TEXT NOT NULL,alias_ref TEXT NOT NULL,lineage TEXT NOT NULL,PRIMARY KEY(merchant,kind,key))",
    "sources(merchant TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(merchant,kind,key,source))",
    "stops(merchant TEXT NOT NULL,store TEXT NOT NULL,target TEXT NOT NULL,ref TEXT NOT NULL,PRIMARY KEY(merchant,store,target,ref))",
    "requests(merchant TEXT NOT NULL,store TEXT NOT NULL,id TEXT NOT NULL,task TEXT NOT NULL,census TEXT NOT NULL,PRIMARY KEY(merchant,store,id))",
    "members(merchant TEXT NOT NULL,store TEXT NOT NULL,request TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','erased','blocked')),blocker TEXT NOT NULL,PRIMARY KEY(merchant,store,request,kind,key))",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("hash_required")
    return value


def identity(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("canonical_uuid_required")
    return value


def resource_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("opaque_resource_key_required")
    return value


def fingerprint(
    store, kind, key, subject, proof, image, alias_kind, alias_ref, alias_proof, sources
):
    return digest(
        [
            store,
            kind,
            key,
            subject,
            proof,
            image,
            alias_kind,
            alias_ref,
            alias_proof,
            sources,
        ]
    )


class Lineage:
    def __init__(
        self, connection, service, merchant, store, *, enabled=False, approved_scopes=()
    ):
        if enabled is not True:
            raise ValueError("c15_disabled")
        if service not in SERVICES:
            raise ValueError("service_required")
        identity(merchant)
        if not isinstance(store, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", store):
            raise ValueError("canonical_store_required")
        if (merchant, store) not in approved_scopes:
            raise ValueError("reviewed_scope_required")
        self.db, self.service, self.m, self.s = connection, service, merchant, store
        self.sqlite = isinstance(connection, sqlite3.Connection)
        if self.sqlite != (service == "wakaru"):
            raise ValueError("service_backend_mismatch")

    def q(self, sql, params=()):
        schema = (
            "main"
            if self.sqlite
            else ("integration_privacy" if self.service == "inkwell" else "public")
        )
        sql = re.sub(
            r"(?<![.\w])integration_lineage_(aliases|resources|sources|stops|requests|members)\b",
            lambda match: schema + "." + match.group(),
            sql,
        )
        return self.db.execute(sql if self.sqlite else sql.replace("?", "%s"), params)

    @contextmanager
    def tx(self):
        # One merchant lock, including all stores. Registration and stop census
        # serialize before touching legacy rows. Never take it in a row trigger.
        if (
            self.db.in_transaction
            if self.sqlite
            else (not self.db.autocommit or self.db.info.transaction_status != 0)
        ):
            raise ValueError("dedicated_idle_autocommit_connection_required")
        if self.sqlite:
            self.q("BEGIN IMMEDIATE")
        else:
            self.q("BEGIN")
        try:
            if not self.sqlite:
                self.q("SELECT set_config('app.current_merchant_id',?,true)", (self.m,))
                self.q(
                    "SELECT pg_advisory_xact_lock(hashtextextended(?,15))", (self.m,)
                )
            yield
            self.q("COMMIT")
        except BaseException:
            self.q("ROLLBACK")
            raise

    def install_sqlite(self):
        if not self.sqlite:
            raise ValueError("use_versioned_postgres_migration")
        with self.tx():
            for definition in DDL:
                self.q("CREATE TABLE IF NOT EXISTS integration_lineage_" + definition)
            for table, key, kind in (
                ("raw_outputs", "id", "raw_output"),
                ("recovery_plans", "revision", "plan"),
            ):
                if not self.q(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    continue
                for operation in ("INSERT", "UPDATE"):
                    # Tenant and object cannot change through an UPDATE. OLD
                    # and NEW are both checked to prevent moving an adopted row.
                    refs = ["NEW"] if operation == "INSERT" else ["OLD", "NEW"]
                    condition = " OR ".join(
                        f"EXISTS(SELECT 1 FROM integration_lineage_resources WHERE merchant={ref}.merchant AND kind='{kind}' AND key={ref}.{key})"
                        for ref in refs
                    )
                    self.q(
                        f"CREATE TRIGGER IF NOT EXISTS integration_lineage_{table}_{operation.lower()} BEFORE {operation} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'c15_registered_record_immutable'); END"
                    )

    def stopped(self, subject, sources):
        if self.q(
            "SELECT 1 FROM integration_lineage_stops WHERE merchant=? AND store=? AND target='subject' AND ref=?",
            (self.m, self.s, subject),
        ).fetchone():
            return True
        return any(
            self.q(
                "SELECT 1 FROM integration_lineage_stops WHERE merchant=? AND store=? AND target='source' AND ref=?",
                (self.m, self.s, source),
            ).fetchone()
            for source in sources
        )

    def bind(self, kind, ref, subject, proof):
        if kind not in KINDS:
            raise ValueError("unsupported_alias_kind")
        token(ref)
        token(subject)
        token(proof)
        with self.tx():
            if self.stopped(subject, ()):
                raise ValueError("subject_stopped")
            old = self.q(
                "SELECT subject,proof FROM integration_lineage_aliases WHERE merchant=? AND store=? AND kind=? AND ref=?",
                (self.m, self.s, kind, ref),
            ).fetchone()
            if old:
                if tuple(old) != (subject, proof):
                    raise ValueError("alias_conflict")
                return
            self.q(
                "INSERT INTO integration_lineage_aliases VALUES(?,?,?,?,?,?)",
                (self.m, self.s, kind, ref, subject, proof),
            )

    def guard(self, kind):
        if self.sqlite:
            table = {"raw_output": "raw_outputs", "plan": "recovery_plans"}.get(kind)
            if table is None:
                raise ValueError("unsupported_adapter")
            names = [
                f"integration_lineage_{table}_{operation}"
                for operation in ("insert", "update")
            ]
            count = self.q(
                "SELECT count(*) FROM main.sqlite_master WHERE type='trigger' AND tbl_name=? AND name IN (?,?)",
                (table, *names),
            ).fetchone()[0]
            if count != 2:
                raise ValueError("adapter_guard_required")
        elif kind != "raw_output":
            raise ValueError("unsupported_adapter")
        elif (
            self.q(
                "SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid='intelligence.raw_outputs'::regclass AND tgname='integration_lineage_raw_guard' AND tgenabled IN ('O','A') AND NOT tgisinternal"
            ).fetchone()[0]
            != 1
        ):
            raise ValueError("adapter_guard_required")

    def payload(self, kind, key, *, lock=False):
        resource_key(key)
        self.guard(kind)
        if kind == "raw_output":
            if self.sqlite:
                sql = "SELECT body FROM raw_outputs WHERE merchant=? AND id=?"
            else:
                # Parent attempt ownership must agree too. No FK/cascade delete
                # of attempts, results, selected judgments, budgets or claims.
                sql = "SELECT r.body FROM intelligence.raw_outputs r WHERE r.merchant_id=? AND r.attempt_id=?"
        elif kind == "plan" and self.sqlite:
            sql = "SELECT payload FROM recovery_plans WHERE merchant=? AND revision=?"
        else:
            raise ValueError("unsupported_adapter")
        if lock and not self.sqlite:
            sql += " FOR UPDATE"
        row = self.q(sql, (self.m, key)).fetchone()
        if row is not None and kind == "raw_output":
            parent = self.q(
                (
                    "SELECT merchant FROM main.attempts WHERE id=?"
                    if self.sqlite
                    else "SELECT merchant_id FROM intelligence.attempts WHERE id=?"
                )
                + (" FOR UPDATE" if lock and not self.sqlite else ""),
                (key,),
            ).fetchone()
            if parent is None or parent[0] != self.m:
                raise ValueError("attempt_ownership_conflict")
        return None if row is None else row[0]

    def adopt(self, kind, key, alias_kind, alias_ref, sources, proof):
        resource_key(key)
        token(alias_ref)
        token(proof)
        if (
            alias_kind not in KINDS
            or not isinstance(sources, list)
            or not 1 <= len(sources) <= 64
            or sources != sorted(set(sources))
        ):
            raise ValueError("explicit_unique_sorted_sources_required")
        for source in sources:
            token(source)
        with self.tx():
            self._adopt_locked(kind, key, alias_kind, alias_ref, sources, proof)

    def _adopt_locked(self, kind, key, alias_kind, alias_ref, sources, proof):
        """Freeze a validated attribution while holding the C15 merchant lock."""
        alias = self.q(
            "SELECT subject,proof FROM integration_lineage_aliases WHERE merchant=? AND store=? AND kind=? AND ref=?",
            (self.m, self.s, alias_kind, alias_ref),
        ).fetchone()
        if not alias:
            raise ValueError("unresolved_alias")
        subject = alias[0]
        if self.stopped(subject, sources):
            raise ValueError("lineage_stopped")
        body = self.payload(kind, key, lock=True)
        if body is None:
            raise ValueError("owned_payload_required")
        if not isinstance(body, str) or len(body.encode()) > 2**20:
            raise ValueError("bounded_text_payload_required")
        image = digest(body)
        frozen = fingerprint(
            self.s,
            kind,
            key,
            subject,
            proof,
            image,
            alias_kind,
            alias_ref,
            alias[1],
            sources,
        )
        old = self.q(
            "SELECT store,subject,proof,image,alias_kind,alias_ref,lineage FROM integration_lineage_resources WHERE merchant=? AND kind=? AND key=?",
            (self.m, kind, key),
        ).fetchone()
        if old:
            previous = [
                r[0]
                for r in self.q(
                    "SELECT source FROM integration_lineage_sources WHERE merchant=? AND kind=? AND key=? ORDER BY source",
                    (self.m, kind, key),
                ).fetchall()
            ]
            if (
                tuple(old)
                != (self.s, subject, proof, image, alias_kind, alias_ref, frozen)
                or previous != sources
            ):
                raise ValueError("resource_lineage_conflict")
            return
        self.q(
            "INSERT INTO integration_lineage_resources VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.m,
                self.s,
                kind,
                key,
                subject,
                proof,
                image,
                alias_kind,
                alias_ref,
                frozen,
            ),
        )
        for source in sources:
            self.q(
                "INSERT INTO integration_lineage_sources VALUES(?,?,?,?,?)",
                (self.m, self.s, kind, key, source),
            )

    def _verified_attributions(self, resource=None):
        """Validate frozen registrations before using their mutable lookup fields."""
        sql = """
            SELECT r.kind,r.key,r.subject,r.proof,r.image,r.alias_kind,r.alias_ref,
                   r.lineage,a.subject,a.proof,s.source
            FROM integration_lineage_resources r
            LEFT JOIN integration_lineage_aliases a
              ON a.merchant=r.merchant AND a.store=r.store AND a.kind=r.alias_kind AND a.ref=r.alias_ref
            LEFT JOIN integration_lineage_sources s
              ON s.merchant=r.merchant AND s.store=r.store AND s.kind=r.kind AND s.key=r.key
            WHERE r.merchant=? AND r.store=?
        """
        params = (self.m, self.s)
        if resource is not None:
            sql += " AND r.kind=? AND r.key=?"
            params += tuple(resource)
        rows = self.q(sql + " ORDER BY r.kind,r.key,s.source", params)
        for attribution, group in groupby(rows, key=lambda row: tuple(row[:-1])):
            (
                kind,
                key,
                subject,
                proof,
                image,
                alias_kind,
                alias_ref,
                frozen,
                alias_subject,
                alias_proof,
            ) = attribution
            sources = [row[-1] for row in group if row[-1] is not None]
            if (
                alias_subject != subject
                or not 1 <= len(sources) <= 64
                or frozen
                != fingerprint(
                    self.s,
                    kind,
                    key,
                    subject,
                    proof,
                    image,
                    alias_kind,
                    alias_ref,
                    alias_proof,
                    sources,
                )
            ):
                raise ValueError("retained_lineage_required")
            yield (kind, key, frozen), subject, sources

    def census(self, keys):
        members = []
        for resource in keys:
            attribution = next(self._verified_attributions(resource), None)
            if attribution is None:
                raise ValueError("retained_lineage_required")
            members.append(attribution[0])
        return digest(members)

    def _scope_members(self, target, ref):
        orphan = self.q(
            """
            SELECT 1 FROM integration_lineage_sources s
            WHERE s.merchant=? AND s.store=? AND NOT EXISTS (
                SELECT 1 FROM integration_lineage_resources r WHERE r.merchant=s.merchant
                AND r.store=s.store AND r.kind=s.kind AND r.key=s.key
            ) LIMIT 1
        """,
            (self.m, self.s),
        ).fetchone()
        if orphan:
            raise ValueError("retained_lineage_required")
        return [
            member
            for member, subject, sources in self._verified_attributions()
            if (subject == ref if target == "subject" else ref in sources)
        ]

    def validate_request(self, request_id):
        row = self.q(
            "SELECT task,census FROM integration_lineage_requests WHERE merchant=? AND store=? AND id=?",
            (self.m, self.s, request_id),
        ).fetchone()
        if row is None:
            raise ValueError("request_required")
        task = json.loads(row[0])
        if (
            not isinstance(task, dict)
            or task.get("target") not in ("subject", "source")
            or row[0]
            != canonical(
                dict(
                    version=VERSION,
                    merchant=self.m,
                    store=self.s,
                    request_id=request_id,
                    target=task.get("target"),
                    ref=task.get("ref"),
                )
            )
        ):
            raise ValueError("retained_lineage_required")
        token(task["ref"])
        if not self.q(
            "SELECT 1 FROM integration_lineage_stops WHERE merchant=? AND store=? AND target=? AND ref=?",
            (self.m, self.s, task["target"], task["ref"]),
        ).fetchone():
            raise ValueError("retained_stop_required")
        keys = self.q(
            "SELECT kind,key FROM integration_lineage_members WHERE merchant=? AND store=? AND request=? ORDER BY kind,key",
            (self.m, self.s, request_id),
        ).fetchall()
        members = self._scope_members(task["target"], task["ref"])
        if [tuple(key) for key in keys] != [member[:2] for member in members] or digest(
            members
        ) != row[1]:
            raise ValueError("retained_lineage_required")
        return row[0], row[1]

    def start(self, request_id, target, ref):
        identity(request_id)
        token(ref)
        if target not in ("subject", "source"):
            raise ValueError("exact_target_required")
        task = canonical(
            dict(
                version=VERSION,
                merchant=self.m,
                store=self.s,
                request_id=request_id,
                target=target,
                ref=ref,
            )
        )
        pending = "0" * 64
        # Commit identity + stop separately: damaged attribution must never
        # roll back the privacy stop. A pending census cannot produce a receipt.
        with self.tx():
            old = self.q(
                "SELECT task,census FROM integration_lineage_requests WHERE merchant=? AND store=? AND id=?",
                (self.m, self.s, request_id),
            ).fetchone()
            if old:
                if old[0] != task:
                    raise ValueError("request_conflict")
                if old[1] != pending:
                    self.validate_request(request_id)
                    return digest(json.loads(task))
                if not self.q(
                    "SELECT 1 FROM integration_lineage_stops WHERE merchant=? AND store=? AND target=? AND ref=?",
                    (self.m, self.s, target, ref),
                ).fetchone():
                    raise ValueError("retained_stop_required")
            else:
                self.q(
                    "INSERT INTO integration_lineage_stops VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                    (self.m, self.s, target, ref),
                )
                self.q(
                    "INSERT INTO integration_lineage_requests VALUES(?,?,?,?,?)",
                    (self.m, self.s, request_id, task, pending),
                )
        with self.tx():
            row = self.q(
                "SELECT census FROM integration_lineage_requests WHERE merchant=? AND store=? AND id=?",
                (self.m, self.s, request_id),
            ).fetchone()
            if row[0] != pending:
                self.validate_request(request_id)
                return digest(json.loads(task))
            if not self.q(
                "SELECT 1 FROM integration_lineage_stops WHERE merchant=? AND store=? AND target=? AND ref=?",
                (self.m, self.s, target, ref),
            ).fetchone():
                raise ValueError("retained_stop_required")
            members = self._scope_members(target, ref)
            frozen = digest(members)
            for kind, key, _ in members:
                self.q(
                    "INSERT INTO integration_lineage_members VALUES(?,?,?,?,?,'pending','')",
                    (self.m, self.s, request_id, kind, key),
                )
            changed = self.q(
                "UPDATE integration_lineage_requests SET census=? WHERE merchant=? AND store=? AND id=? AND census=?",
                (frozen, self.m, self.s, request_id, pending),
            ).rowcount
            if changed != 1:
                raise ValueError("census_compare_and_set_failed")
        return digest(json.loads(task))

    def read(self, kind, key):
        with self.tx():
            return self._read_locked(kind, key)

    def _read_locked(self, kind, key):
        row = self.q(
            "SELECT subject,image FROM integration_lineage_resources WHERE merchant=? AND store=? AND kind=? AND key=?",
            (self.m, self.s, kind, key),
        ).fetchone()
        if not row:
            raise ValueError("registered_record_required")
        sources = [
            r[0]
            for r in self.q(
                "SELECT source FROM integration_lineage_sources WHERE merchant=? AND store=? AND kind=? AND key=?",
                (self.m, self.s, kind, key),
            )
        ]
        if self.stopped(row[0], sources):
            raise ValueError("lineage_stopped")
        self.census([(kind, key)])
        body = self.payload(kind, key, lock=True)
        if body is None or digest(body) != row[1]:
            raise ValueError("payload_missing_or_changed")
        return body

    def drain(self, request_id, *, limit=100):
        identity(request_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded_page_required")
        with self.tx():
            self.validate_request(request_id)
            rows = self.q(
                "SELECT kind,key,state FROM integration_lineage_members WHERE merchant=? AND store=? AND request=? AND state<>'erased' ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END,kind,key LIMIT ?",
                (self.m, self.s, request_id, limit),
            ).fetchall()
            for kind, key, state in rows:
                expected = self.q(
                    "SELECT image FROM integration_lineage_resources WHERE merchant=? AND store=? AND kind=? AND key=?",
                    (self.m, self.s, kind, key),
                ).fetchone()[0]
                body = self.payload(kind, key, lock=True)
                # A prior, committed erasure of the same exact row by another
                # request is sufficient. Unexplained absence never is.
                accounted = self.q(
                    "SELECT 1 FROM integration_lineage_members WHERE merchant=? AND store=? AND kind=? AND key=? AND state='erased'",
                    (self.m, self.s, kind, key),
                ).fetchone()
                blocker = (
                    ""
                    if body is not None and digest(body) == expected
                    else (
                        ""
                        if body is None and accounted
                        else "payload_missing_or_changed"
                    )
                )
                if not blocker and body is not None:
                    table, column, tenant = (
                        ("recovery_plans", "revision", "merchant")
                        if kind == "plan"
                        else (
                            ("raw_outputs", "id", "merchant")
                            if self.sqlite
                            else (
                                "intelligence.raw_outputs",
                                "attempt_id",
                                "merchant_id",
                            )
                        )
                    )
                    cursor = self.q(
                        f"DELETE FROM {table} WHERE {tenant}=? AND {column}=?",
                        (self.m, key),
                    )
                    if cursor.rowcount != 1 or self.payload(kind, key) is not None:
                        raise RuntimeError("erasure_postcondition")
                self.q(
                    "UPDATE integration_lineage_members SET state=?,blocker=? WHERE merchant=? AND store=? AND request=? AND kind=? AND key=?",
                    (
                        "blocked" if blocker else "erased",
                        blocker,
                        self.m,
                        self.s,
                        request_id,
                        kind,
                        key,
                    ),
                )
        return self.receipt(request_id)

    def receipt(self, request_id):
        identity(request_id)
        with self.tx():
            task, frozen = self.validate_request(request_id)
            rows = self.q(
                "SELECT kind,key,state,blocker FROM integration_lineage_members WHERE merchant=? AND store=? AND request=? ORDER BY kind,key",
                (self.m, self.s, request_id),
            ).fetchall()
            # Reconcile even completed members after a stale payload restore.
            # These reads never return payloads. Do not trust a saved receipt.
            for kind, key, state, blocker in rows:
                if state == "erased" and self.payload(kind, key, lock=True) is not None:
                    self.q(
                        "UPDATE integration_lineage_members SET state='pending',blocker='' WHERE merchant=? AND store=? AND request=? AND kind=? AND key=?",
                        (self.m, self.s, request_id, kind, key),
                    )
            rows = self.q(
                "SELECT kind,key,state,blocker FROM integration_lineage_members WHERE merchant=? AND store=? AND request=? ORDER BY kind,key",
                (self.m, self.s, request_id),
            ).fetchall()
            matched, erased = len(rows), sum(r[2] == "erased" for r in rows)
            blockers = sorted({r[3] for r in rows if r[3]})
            if not matched:
                blockers.append("unresolved_registered_coverage")
            return dict(
                version=VERSION,
                service=self.service,
                merchant=self.m,
                store=self.s,
                request_id=request_id,
                task_sha256=digest(json.loads(task)),
                census_sha256=frozen,
                matched=matched,
                erased=erased,
                remaining=matched - erased,
                blockers=blockers,
                registered_scope_complete=bool(
                    matched and matched == erased and not blockers
                ),
                whole_application_erasure_complete=False,
                live_ingestion_enabled=False,
                deployment_enabled=False,
                email_sends_enabled=False,
            )
