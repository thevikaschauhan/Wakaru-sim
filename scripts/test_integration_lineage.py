"""Actual SQL/SQLite C15 operator behavior, including retained-ledger restore.
Run SQLite unconditionally. Postgres requires an explicit fresh loopback
c15_fixture_test database, supplied by the disposable rehearsal only.
"""

import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
import uuid
from integration_lineage import Lineage, digest

M = "10000000-0000-4000-8000-000000000001"
OTHER = "20000000-0000-4000-8000-000000000002"
A, SUBJECT, SOURCE, PROOF = [digest(x) for x in ("alias", "subject", "source", "proof")]


class Behavior:
    def operator(self, *, merchant=M, store="17"):
        return Lineage(
            self.db,
            self.service,
            merchant,
            store,
            enabled=True,
            approved_scopes=((merchant, store),),
        )

    def seed(self, key="attempt-1", merchant=M, body="private buyer text"):
        if self.service == "wakaru":
            self.db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?)", (key, merchant, key, 1, 2)
            )
            self.db.execute(
                "INSERT INTO raw_outputs VALUES(?,?,?,?)", (key, merchant, body, 1)
            )
        else:
            self.db.execute(
                "SELECT set_config('app.current_merchant_id',%s,false)", (merchant,)
            )
            self.db.execute(
                "INSERT INTO intelligence.attempts VALUES(%s,%s,%s,now(),2)",
                (key, merchant, key),
            )
            self.db.execute(
                "INSERT INTO intelligence.raw_outputs VALUES(%s,%s,%s,now())",
                (key, merchant, body),
            )

    def adopt(self, key="attempt-1", sources=None):
        op = self.operator()
        op.bind("shopify_customer", A, SUBJECT, PROOF)
        op.adopt("raw_output", key, "shopify_customer", A, sources or [SOURCE], PROOF)
        return op

    def request(self, op, target="subject", ref=SUBJECT):
        request = str(uuid.uuid4())
        op.start(request, target, ref)
        return request

    def test_disabled_and_unreviewed_scope(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            Lineage(None, self.service, M, "17")
        with self.assertRaisesRegex(ValueError, "reviewed_scope"):
            Lineage(None, self.service, M, "17", enabled=True)

    def test_actual_content_erasure_preserves_attempts_and_fences_replay(self):
        self.seed()
        op = self.adopt()
        self.assertEqual(op.read("raw_output", "attempt-1"), "private buyer text")
        request = self.request(op)
        with self.assertRaisesRegex(ValueError, "stopped"):
            op.read("raw_output", "attempt-1")
        receipt = op.drain(request)
        self.assertTrue(receipt["registered_scope_complete"])
        self.assertEqual(
            (receipt["matched"], receipt["erased"], receipt["remaining"]), (1, 1, 0)
        )
        self.assertFalse(receipt["whole_application_erasure_complete"])
        table = "attempts" if self.service == "wakaru" else "intelligence.attempts"
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 1
        )
        self.assertEqual(op.drain(request), receipt)
        with self.assertRaises(Exception):
            if self.service == "wakaru":
                self.db.execute(
                    "INSERT INTO raw_outputs VALUES(?,?,?,1)",
                    ("attempt-1", M, "replay"),
                )
            else:
                self.db.execute(
                    "INSERT INTO intelligence.raw_outputs VALUES(%s,%s,'replay',now())",
                    ("attempt-1", M),
                )

    def test_alias_no_email_merge_conflict_and_cross_store_adoption(self):
        self.seed()
        op = self.adopt()
        with self.assertRaisesRegex(ValueError, "unsupported_alias"):
            op.bind("email", A, SUBJECT, PROOF)
        with self.assertRaisesRegex(ValueError, "alias_conflict"):
            op.bind("shopify_customer", A, digest("other subject"), PROOF)
        other = self.operator(store="18")
        other.bind("shopify_customer", A, SUBJECT, PROOF)
        with self.assertRaisesRegex(ValueError, "resource_lineage_conflict"):
            other.adopt(
                "raw_output", "attempt-1", "shopify_customer", A, [SOURCE], PROOF
            )
        other = self.operator(merchant=OTHER)
        other.bind("shopify_customer", A, SUBJECT, PROOF)
        with self.assertRaisesRegex(ValueError, "owned_payload_required"):
            other.adopt(
                "raw_output", "attempt-1", "shopify_customer", A, [SOURCE], PROOF
            )
        self.assertEqual(op.read("raw_output", "attempt-1"), "private buyer text")

    def test_exact_source_withdrawal_leaves_other_revision(self):
        self.seed()
        self.seed("attempt-2")
        op = self.adopt()
        self.adopt("attempt-2", [digest("new revision")])
        request = self.request(op, "source", SOURCE)
        self.assertEqual(op.drain(request)["matched"], 1)
        self.assertEqual(op.read("raw_output", "attempt-2"), "private buyer text")
        self.seed("attempt-3")
        with self.assertRaisesRegex(ValueError, "stopped"):
            op.adopt("raw_output", "attempt-3", "shopify_customer", A, [SOURCE], PROOF)

    def test_unresolved_before_arrival_and_request_conflicts(self):
        op = self.operator()
        request = self.request(op)
        self.assertEqual(
            op.drain(request)["blockers"], ["unresolved_registered_coverage"]
        )
        with self.assertRaisesRegex(ValueError, "stopped"):
            op.bind("shopify_customer", A, SUBJECT, PROOF)
        with self.assertRaisesRegex(ValueError, "request_conflict"):
            op.start(request, "source", SOURCE)
        op.start(request, "subject", SUBJECT)

    def test_unexplained_absence_is_blocked_but_overlapping_receipt_counts(self):
        self.seed()
        op = self.adopt()
        request = self.request(op)
        table = (
            "raw_outputs" if self.service == "wakaru" else "intelligence.raw_outputs"
        )
        self.db.execute("DELETE FROM " + table)
        receipt = op.drain(request)
        self.assertFalse(receipt["registered_scope_complete"])
        self.assertEqual(receipt["blockers"], ["payload_missing_or_changed"])

    def test_overlapping_subject_and_source_requests(self):
        self.seed()
        op = self.adopt()
        first = self.request(op, "source", SOURCE)
        second = self.request(op)
        self.assertTrue(op.drain(first)["registered_scope_complete"])
        self.assertTrue(op.drain(second)["registered_scope_complete"])

    def test_sql_failure_keeps_committed_stop_and_allows_retry(self):
        self.seed()
        op = self.adopt()
        request = self.request(op)
        if self.service == "wakaru":
            self.db.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON raw_outputs BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        else:
            self.db.execute(
                "CREATE FUNCTION fail_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$"
            )
            self.db.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON intelligence.raw_outputs FOR EACH ROW EXECUTE FUNCTION fail_delete()"
            )
        with self.assertRaises(Exception):
            op.drain(request)
        with self.assertRaisesRegex(ValueError, "stopped"):
            op.read("raw_output", "attempt-1")
        self.assertEqual(op.receipt(request)["erased"], 0)
        self.db.execute(
            "DROP TRIGGER fail_delete"
            + ("" if self.service == "wakaru" else " ON intelligence.raw_outputs")
        )
        self.assertTrue(op.drain(request)["registered_scope_complete"])

    def test_registered_content_update_and_tenant_move_are_rejected(self):
        self.seed()
        self.adopt()
        sql = (
            "UPDATE raw_outputs SET body='changed'"
            if self.service == "wakaru"
            else "UPDATE intelligence.raw_outputs SET body='changed'"
        )
        with self.assertRaises(Exception):
            self.db.execute(sql)
        sql = (
            "UPDATE raw_outputs SET merchant='" + OTHER + "'"
            if self.service == "wakaru"
            else "UPDATE intelligence.raw_outputs SET merchant_id='" + OTHER + "'"
        )
        with self.assertRaises(Exception):
            self.db.execute(sql)

    def test_paged_fanout_repeated_requests_and_scope_isolation(self):
        op = self.operator()
        op.bind("shopify_customer", A, SUBJECT, PROOF)
        for i in range(1007):
            self.seed(str(i))
            op.adopt("raw_output", str(i), "shopify_customer", A, [SOURCE], PROOF)
        self.seed("unregistered")
        self.seed("other-merchant", OTHER)
        request = self.request(op)
        self.assertEqual(op.drain(request)["erased"], 100)
        self.assertEqual(op.drain(request)["erased"], 200)
        receipt = op.drain(request)
        while receipt["remaining"]:
            receipt = op.drain(request)
        self.assertEqual(receipt["erased"], 1007)
        self.assertTrue(receipt["registered_scope_complete"])
        self.assertEqual(op.payload("raw_output", "unregistered"), "private buyer text")
        self.assertEqual(
            self.operator(merchant=OTHER).payload("raw_output", "other-merchant"),
            "private buyer text",
        )

    def test_live_outer_transaction_is_not_committed(self):
        op = self.operator()
        self.db.execute("BEGIN")
        try:
            with self.assertRaisesRegex(ValueError, "dedicated_idle"):
                op.bind("shopify_customer", A, SUBJECT, PROOF)
        finally:
            self.db.execute("ROLLBACK")

    def test_parent_attempt_ownership_cannot_be_substituted(self):
        self.seed()
        op = self.adopt()
        column = "merchant" if self.service == "wakaru" else "merchant_id"
        table = "attempts" if self.service == "wakaru" else "intelligence.attempts"
        marker = "?" if self.service == "wakaru" else "%s"
        self.db.execute("UPDATE " + table + " SET " + column + "=" + marker, (OTHER,))
        with self.assertRaisesRegex(ValueError, "attempt_ownership_conflict"):
            op.read("raw_output", "attempt-1")

    def test_attribution_failure_commits_stop_and_can_resume(self):
        self.seed()
        self.seed("attempt-2")
        op = self.adopt()
        self.adopt("attempt-2")
        request = str(uuid.uuid4())
        op.q("DELETE FROM integration_lineage_sources WHERE key='attempt-2'")
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.start(request, "source", SOURCE)
        with self.assertRaisesRegex(ValueError, "lineage_stopped"):
            op.read("raw_output", "attempt-1")
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.drain(request)
        op.q(
            "INSERT INTO integration_lineage_sources VALUES(?,?,?,?,?)",
            (M, "17", "raw_output", "attempt-2", SOURCE),
        )
        op.start(request, "source", SOURCE)
        receipt = op.drain(request)
        self.assertEqual((receipt["matched"], receipt["erased"]), (2, 2))
        self.assertTrue(receipt["registered_scope_complete"])

        # A historical receipt can contain a self-consistent but narrowed census.
        op.q("DELETE FROM integration_lineage_members WHERE key='attempt-2'")
        op.q(
            "UPDATE integration_lineage_requests SET census=? WHERE id=?",
            (op.census([("raw_output", "attempt-1")]), request),
        )
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.receipt(request)

    def test_read_rejects_attribution_drift_before_exposing_payload(self):
        self.seed()
        op = self.adopt()
        op.q("DELETE FROM integration_lineage_sources")
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.read("raw_output", "attempt-1")

    def test_census_changes_when_source_membership_changes(self):
        self.seed()
        op = self.adopt()
        first = op.receipt(self.request(op))["census_sha256"]
        self.seed("attempt-2")
        other = self.operator(store="18")
        other.bind("shopify_customer", A, SUBJECT, PROOF)
        other.adopt(
            "raw_output",
            "attempt-2",
            "shopify_customer",
            A,
            [digest("other source")],
            PROOF,
        )
        second = other.receipt(self.request(other))["census_sha256"]
        self.assertNotEqual(first, second)


class SQLiteTests(Behavior, unittest.TestCase):
    service = "wakaru"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "legacy.sqlite"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.executescript(
            "CREATE TABLE attempts(id TEXT PRIMARY KEY,merchant TEXT,request_key TEXT,started REAL,reserved REAL);CREATE TABLE raw_outputs(id TEXT PRIMARY KEY,merchant TEXT,body TEXT,created REAL);CREATE TABLE recovery_plans(merchant TEXT,revision TEXT,payload TEXT,created REAL,PRIMARY KEY(merchant,revision));"
        )
        self.operator().install_sqlite()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_missing_retained_source_never_narrows_completion(self):
        self.seed()
        op = self.adopt()
        request = self.request(op)
        self.assertTrue(op.drain(request)["registered_scope_complete"])
        self.db.execute("DELETE FROM integration_lineage_sources")
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.receipt(request)

    def test_missing_retained_member_never_narrows_completion(self):
        self.seed()
        self.seed("attempt-2")
        op = self.adopt()
        self.adopt("attempt-2")
        request = self.request(op)
        self.assertTrue(op.drain(request)["registered_scope_complete"])
        self.db.execute("DELETE FROM integration_lineage_members WHERE key='attempt-2'")
        with self.assertRaisesRegex(ValueError, "retained_lineage_required"):
            op.receipt(request)

    def test_missing_retained_stop_never_completes(self):
        self.seed()
        op = self.adopt()
        request = self.request(op)
        self.assertTrue(op.drain(request)["registered_scope_complete"])
        self.db.execute("DELETE FROM integration_lineage_stops")
        with self.assertRaisesRegex(ValueError, "retained_stop_required"):
            op.receipt(request)

    def test_table_created_after_install_requires_guard_enrollment(self):
        self.db.execute("DROP TABLE recovery_plans")
        self.db.execute(
            "CREATE TABLE recovery_plans(merchant TEXT,revision TEXT,payload TEXT,created REAL,PRIMARY KEY(merchant,revision))"
        )
        self.db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,1)",
            (M, "late-plan", "private plan"),
        )
        op = self.operator()
        op.bind("cart_id", A, SUBJECT, PROOF)
        with self.assertRaisesRegex(ValueError, "adapter_guard_required"):
            op.adopt("plan", "late-plan", "cart_id", A, [SOURCE], PROOF)
        op.install_sqlite()
        op.adopt("plan", "late-plan", "cart_id", A, [SOURCE], PROOF)
        self.assertTrue(op.drain(self.request(op))["registered_scope_complete"])

    def test_plan_payload_erase_preserves_stop_and_replay_guard(self):
        self.db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,1)",
            (M, "revision-1", "private plan"),
        )
        op = self.operator()
        op.bind("cart_id", A, SUBJECT, PROOF)
        op.adopt("plan", "revision-1", "cart_id", A, [SOURCE], PROOF)
        self.assertTrue(op.drain(self.request(op))["registered_scope_complete"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT OR IGNORE INTO recovery_plans VALUES(?,?,?,1)",
                (M, "revision-1", "replay"),
            )

    def test_actual_sqlite_backup_stale_payload_latest_ledger_reopens_receipt(self):
        self.seed()
        op = self.adopt()
        backup = sqlite3.connect(
            Path(self.temp.name) / "old.sqlite", isolation_level=None
        )
        self.db.backup(backup)
        request = self.request(op)
        self.assertTrue(op.drain(request)["registered_scope_complete"])
        ledger = {
            t: self.db.execute("SELECT * FROM integration_lineage_" + t).fetchall()
            for t in ("aliases", "resources", "sources", "stops", "requests", "members")
        }
        restored = sqlite3.connect(
            Path(self.temp.name) / "restore.sqlite", isolation_level=None
        )
        backup.backup(restored)
        backup.close()
        for table, rows in ledger.items():
            restored.execute("DELETE FROM integration_lineage_" + table)
            for row in rows:
                restored.execute(
                    "INSERT INTO integration_lineage_"
                    + table
                    + " VALUES("
                    + ",".join("?" for _ in row)
                    + ")",
                    row,
                )
        rop = Lineage(
            restored, "wakaru", M, "17", enabled=True, approved_scopes=((M, "17"),)
        )
        with self.assertRaisesRegex(ValueError, "stopped"):
            rop.read("raw_output", "attempt-1")
        self.assertEqual(rop.receipt(request)["remaining"], 1)
        self.assertTrue(rop.drain(request)["registered_scope_complete"])
        restored.close()

    def test_stop_races_with_adoption_without_losing_the_member(self):
        self.seed()
        op = self.operator()
        op.bind("shopify_customer", A, SUBJECT, PROOF)
        barrier = threading.Barrier(2)
        result = []

        def adopt():
            with sqlite3.connect(self.path, timeout=5, isolation_level=None) as db:
                other = Lineage(
                    db, "wakaru", M, "17", enabled=True, approved_scopes=((M, "17"),)
                )
                barrier.wait()
                try:
                    other.adopt(
                        "raw_output",
                        "attempt-1",
                        "shopify_customer",
                        A,
                        [SOURCE],
                        PROOF,
                    )
                    result.append("adopted")
                except ValueError as error:
                    result.append(str(error))

        thread = threading.Thread(target=adopt)
        thread.start()
        barrier.wait()
        request = self.request(op)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        receipt = op.drain(request)
        self.assertEqual(
            result, ["adopted"] if receipt["matched"] else ["lineage_stopped"]
        )
        if receipt["matched"]:
            self.assertTrue(receipt["registered_scope_complete"])


@unittest.skipUnless(
    os.environ.get("C15_FIXTURE_DSN"), "explicit disposable PostgreSQL fixture required"
)
class PostgresTests(Behavior, unittest.TestCase):
    service = os.environ.get("C15_SERVICE", "engine")

    def setUp(self):
        import psycopg

        dsn = os.environ["C15_FIXTURE_DSN"]
        url = urlsplit(dsn)
        if (
            url.hostname != "127.0.0.1"
            or url.path != "/c15_fixture_test"
            or url.query not in ("", "sslmode=disable")
            or url.fragment
        ):
            raise ValueError("fresh_loopback_fixture_required")
        self.db = psycopg.connect(dsn, autocommit=True)
        self.db.execute(
            "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='inkwell_app') THEN CREATE ROLE inkwell_app NOLOGIN; END IF; END $$"
        )
        # A fresh explicitly named test DB only. Never accepts another DB name.
        self.db.execute(
            "DROP SCHEMA IF EXISTS intelligence CASCADE; DROP SCHEMA IF EXISTS integration_privacy CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public"
        )
        migration = (
            "054_typesafe_intelligence.sql"
            if self.service == "engine"
            else "0047_intelligence.sql"
        )
        self.db.execute(
            (Path(__file__).parents[1] / "migrations" / migration)
            .read_text()
            .split("-- +goose Down")[0]
        )
        if self.service == "inkwell":
            self.db.execute("CREATE SCHEMA integration_privacy")
        path = (
            "092_integration_lineage.sql"
            if self.service == "engine"
            else "0066_integration_lineage.sql"
        )
        self.db.execute(
            (Path(__file__).parents[1] / "migrations" / path)
            .read_text()
            .split("-- +goose Down")[0]
        )

    def test_postgres_stop_races_with_adoption(self):
        import psycopg

        self.seed()
        op = self.operator()
        op.bind("shopify_customer", A, SUBJECT, PROOF)
        barrier = threading.Barrier(2)
        result = []

        def adopt():
            with psycopg.connect(os.environ["C15_FIXTURE_DSN"], autocommit=True) as db:
                other = Lineage(
                    db,
                    self.service,
                    M,
                    "17",
                    enabled=True,
                    approved_scopes=((M, "17"),),
                )
                barrier.wait()
                try:
                    other.adopt(
                        "raw_output",
                        "attempt-1",
                        "shopify_customer",
                        A,
                        [SOURCE],
                        PROOF,
                    )
                    result.append("adopted")
                except ValueError as error:
                    result.append(str(error))

        thread = threading.Thread(target=adopt)
        thread.start()
        barrier.wait()
        request = self.request(op)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        receipt = op.drain(request)
        self.assertEqual(
            result, ["adopted"] if receipt["matched"] else ["lineage_stopped"]
        )
        if receipt["matched"]:
            self.assertTrue(receipt["registered_scope_complete"])

    def test_lock_timeout_rolls_back_transaction_setup(self):
        import psycopg

        with psycopg.connect(os.environ["C15_FIXTURE_DSN"], autocommit=True) as holder:
            holder.execute("BEGIN")
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,15))", (M,)
            )
            self.db.execute("SET lock_timeout='20ms'")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.operator().bind("shopify_customer", A, SUBJECT, PROOF)
            holder.execute("ROLLBACK")
        self.assertEqual(self.db.info.transaction_status, 0)

    def tearDown(self):
        self.db.close()


if __name__ == "__main__":
    unittest.main()
