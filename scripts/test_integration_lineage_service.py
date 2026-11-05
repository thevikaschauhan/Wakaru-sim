"""C16 synthetic signed HTTP through the actual SQLite producer and plan cache."""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

import httpx
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cart_recovery.buyer_state import direct_insight
from cart_recovery.shopify_formatter import ShopifyCartData
from cart_recovery.intelligence.recovery import PlanJournal, RecoveryWorkflow
from cart_recovery.intelligence.typesafe import Client, Store, digest as judgment_digest
from cart_recovery.lineage_client import OfflineLineageClient
from integration_lineage import Lineage, digest
from integration_lineage_service import (
    ADMIT,
    CAPTURE,
    READ,
    Binding,
    create_lineage_app,
    enroll_sqlite_file,
)

M = "60000000-0000-4000-8000-000000000016"
SCOPE = (M, "17")
A, S, P, SRC = map(digest, ("alias", "subject", "proof", "source"))
BINDING = Binding("cart_id", A, S, P, (SRC,), P)


class AuthenticatedPaths(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "legacy.sqlite"
        self.fixture = json.loads(
            (ROOT / "contracts/intelligence/v1/conformance.json").read_text()
        )[0]
        self.calls = 0

        def synthetic(request):
            self.calls += 1
            return httpx.Response(200, json=self.fixture["response"])

        client = Client(
            key="synthetic-only",
            model="fixture",
            approved_models=[self.fixture["response"]["model"]],
            transport=httpx.MockTransport(synthetic),
        )
        self.addCleanup(client.close)
        self.store = Store(self.path, client)
        self.journal = PlanJournal(self.path)
        self.db = self.connect(*SCOPE)
        self.addCleanup(self.db.close)
        self.op = Lineage(
            self.db, "wakaru", *SCOPE, enabled=True, approved_scopes=[SCOPE]
        )
        self.op.install_sqlite()
        enroll_sqlite_file(self.op, ("raw_output", "plan"))
        self.op.bind("cart_id", A, S, P)
        self.grant = dict(
            key_id="native",
            secret="fixture-only-" + "x" * 32,
            merchant_id=M,
            store_id="17",
            audience="wakaru",
            operations=["POST " + p for p in (ADMIT, CAPTURE, READ)],
        )

    def connect(self, merchant, store):
        return sqlite3.connect(self.path, isolation_level=None)

    def app(self, bindings, **kwargs):
        return create_lineage_app(
            "wakaru",
            enabled=True,
            bindings=bindings,
            grants=kwargs.get("grants", [self.grant]),
            connect=self.connect,
        )

    @contextmanager
    def server(self, bindings):
        server = make_server("127.0.0.1", 0, self.app(bindings))
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield OfflineLineageClient(
                f"http://127.0.0.1:{server.server_port}", self.grant
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_native_capture_and_restarted_plan_cache(self):
        cart = ShopifyCartData("synthetic", "", "", [], 0, episode_id="offline")
        original = direct_insight(cart)
        # Derive the same plan revision without touching a journal.
        preview = RecoveryWorkflow(SimpleNamespace(client=self.store.client)).run(
            cart, original, M, "enforce"
        )
        revision = preview.recovery_plan["revision"]
        identity = dict(
            merchant_id=M,
            workflow="c16_fixture",
            revision="revision",
            snapshot_hash="snapshot",
            rubric_hash="rubric",
            policy_version="policy",
            candidate_hash="candidate",
            as_of="2026-01-01",
        )
        req = self.fixture["request"]
        key = judgment_digest(
            [v for k, v in identity.items() if k != "as_of"]
            + [
                judgment_digest({**req, "model": self.store.client.model}),
                self.store.client.binding,
            ]
        )
        bindings = {
            (*SCOPE, "plan", revision): BINDING,
            (*SCOPE, "raw_output", key): BINDING,
            (*SCOPE, "raw_output", "other-request"): BINDING,
        }
        with self.server(bindings) as client:
            self.store.lineage = client
            result = self.store.judge(identity, req)
            attempt = result["result"]["attempts"][0]["id"]
            raw = client.request(M, "raw_output", attempt)
            self.assertEqual(json.loads(raw), self.fixture["response"])
            admission = client.request(M, "raw_output", key, admit=True)
            client.request(M, "raw_output", attempt, raw, binding=admission)
            with self.assertRaises(ValueError):
                client.request(M, "raw_output", "other-request", admit=True,
                               attempt_id=attempt)
            # A protected selection predating C16 cannot use bare enrollment to
            # stand in for its absent frozen attempt. Restore the genuine fixture
            # afterward to exercise cache replay following source withdrawal.
            saved, = self.db.execute("SELECT judgment FROM selected").fetchone()
            historical = json.loads(saved)
            historical["result"]["attempts"][-1]["id"] = str(uuid4())
            self.db.execute("UPDATE selected SET judgment=?", (json.dumps(historical),))
            with self.assertRaises(ValueError):
                self.store.judge(identity, req)
            self.db.execute("UPDATE selected SET judgment=?", (saved,))
            self.assertIsNone(client.load(M, revision))
            with self.assertRaisesRegex(ValueError, "authenticated_plan"):
                self.journal.select(M, revision, {}, {})
            with self.assertRaises(sqlite3.OperationalError):
                self.db.execute(
                    "INSERT INTO raw_outputs VALUES('bypass',?,?,1)", (M, raw)
                )
            with self.assertRaisesRegex(ValueError, "authenticated_raw"):
                Store(self.path, self.store.client).judge(identity, req)
            first = RecoveryWorkflow(self.store).run(cart, original, M, "enforce")
            self.assertEqual(first.recovery_plan["revision"], revision)
        with self.server(bindings) as client:
            self.store.lineage = client
            self.assertEqual(
                RecoveryWorkflow(self.store).run(cart, original, M, "enforce"), first
            )
            with self.assertRaisesRegex(ValueError, "authenticated_plan"):
                self.journal.load(M, revision)
            self.op.start(str(uuid4()), "source", SRC)
            for action in (
                lambda: client.request(M, "raw_output", attempt),
                lambda: self.store.judge(identity, req),
                lambda: RecoveryWorkflow(self.store).run(cart, original, M, "enforce"),
                lambda: client.select(M, revision, {}, {}),
            ):
                with self.assertRaises(ValueError):
                    action()
            self.assertEqual(self.calls, 1)
        # Restart with the same request key rebound to another reviewed source.
        # Raw reads AND the native selected cache must honor the original stop.
        replacement = replace(BINDING, sources=(digest("other-source"),))
        with self.server({(*SCOPE, "raw_output", key): replacement}) as client:
            self.store.lineage = client
            current = client.request(M, "raw_output", key, admit=True)
            self.assertNotEqual(current, admission)
            with self.assertRaises(ValueError):
                client.request(M, "raw_output", attempt)
            with self.assertRaises(ValueError):
                self.store.judge(identity, req)
            self.assertEqual(self.calls, 1)
            with self.assertRaises(ValueError):
                client.request(M, "raw_output", key, admit=True, binding=admission)
            # A delayed response for a distinct, previously admitted attempt must
            # not register a raw body against the replacement source.
            late = str(uuid4())
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?)",
                            (late, M, key, time.time(), 0))
            with self.assertRaises(ValueError):
                client.request(M, "raw_output", late, raw, binding=admission)
            self.assertIsNone(self.db.execute("SELECT 1 FROM raw_outputs WHERE id=?", (late,)).fetchone())

    def test_plan_miss_cannot_capture_after_rebinding(self):
        rev = "plan-revision"
        key = (*SCOPE, "plan", rev)
        with self.server({key: BINDING}) as client:
            self.assertIsNone(client.load(M, rev))
            value, original = client.lookup_plan(M, rev)
            self.assertIsNone(value)
        with self.server({key: replace(BINDING, sources=(digest("new-source"),))}) as client:
            with self.assertRaises(ValueError):
                client.select(M, rev, {}, {}, binding=original)
            self.assertIsNone(self.db.execute("SELECT 1 FROM recovery_plans").fetchone())

    def test_rejected_answers_stay_out_of_judgment_history_and_return(self):
        identity = dict(merchant_id=M, workflow="fixture", revision="revision",
                        snapshot_hash="snapshot", rubric_hash="rubric",
                        policy_version="policy", candidate_hash="candidate",
                        as_of="2026-01-01")
        req = self.fixture["request"]
        key = judgment_digest(
            [v for k, v in identity.items() if k != "as_of"]
            + [judgment_digest({**req, "model": self.store.client.model}), self.store.client.binding]
        )
        approved_model = self.fixture["response"]["model"]
        with self.server({(*SCOPE, "raw_output", key): BINDING}) as client:
            self.store.lineage = client
            for model, cost, reason in [
                ("unapproved", 0.00001, "unapproved_model"),
                (approved_model, 0.01, "cost_reservation_exceeded"),
            ]:
                self.fixture["response"].update(model=model, usage={"cost": cost})
                result = self.store.judge(identity, req)["result"]
                self.assertEqual((result["status"], result["error_class"]), ("unresolved", reason))
                self.assertNotIn("response", result)
                attempt, = result["attempts"]
                self.assertEqual(attempt["cost"], cost)
                stored = json.loads(self.db.execute(
                    "SELECT record FROM judgments ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0])
                self.assertNotIn("response", stored["result"])
                raw = json.loads(client.request(M, "raw_output", attempt["id"]))
                self.assertEqual(raw["answers"], self.fixture["response"]["answers"])
            self.assertEqual(self.db.execute("SELECT count(*) FROM selected").fetchone(), (0,))

    def test_ambiguous_store_and_shared_credentials_rejected_before_open(self):
        second = dict(
            self.grant, key_id="second", store_id="18", secret="second-" + "b" * 32
        )
        bindings = {(M, store, "plan", "revision"): BINDING for store in ("17", "18")}
        with self.assertRaisesRegex(ValueError, "ambiguous_native_store_binding"):
            self.app(bindings, grants=[self.grant, second])
        with self.assertRaisesRegex(ValueError, "grant_separation"):
            self.app(
                {(*SCOPE, "plan", "rev"): BINDING},
                grants=[self.grant, dict(second, secret=self.grant["secret"])],
            )

    def test_authentication_and_partial_write(self):
        assert create_lineage_app("wakaru").test_client().post(READ).status_code == 404
        bindings = {(*SCOPE, "plan", "rev"): BINDING}
        reader = dict(
            self.grant,
            key_id="reader",
            secret="reader-only-" + "r" * 32,
            operations=["POST " + READ],
        )
        app = self.app(bindings, grants=[self.grant, reader]).test_client()

        def call(path, value, *, grant=None, stamp=None, sign_path=None, wire=None):
            grant = grant or self.grant
            raw = json.dumps(value).encode()
            stamp = str(int(time.time()) if stamp is None else stamp)
            msg = "\n".join(
                (
                    "integration_service_request_v1",
                    grant["key_id"],
                    grant["audience"],
                    grant["merchant_id"],
                    grant["store_id"],
                    "POST",
                    sign_path or path,
                    stamp,
                    hashlib.sha256(raw).hexdigest(),
                )
            )
            sig = hmac.new(
                grant["secret"].encode(), msg.encode(), hashlib.sha256
            ).hexdigest()
            return app.post(
                path,
                data=raw if wire is None else wire,
                headers={
                    "Authorization": f"Integration v1.{grant['key_id']}.{stamp}.{sig}"
                },
            )

        read = dict(merchant_id=M, kind="plan", key="rev")
        admission = call(READ, read, grant=reader).json["binding"]
        value = dict(read, body="private", binding=admission)
        for kwargs in (
            dict(grant=reader),
            dict(stamp=int(time.time()) - 31),
            dict(sign_path=READ),
            dict(wire=b"{}"),
            dict(grant=dict(self.grant, store_id="18")),
            dict(grant=dict(self.grant, audience="engine")),
        ):
            self.assertEqual(call(CAPTURE, value, **kwargs).status_code, 401)
        self.assertEqual(
            call(CAPTURE, dict(value, merchant_id=str(uuid4()))).status_code, 400
        )
        self.assertEqual(call(CAPTURE, dict(value, sources=[])).status_code, 400)
        self.assertEqual(call(CAPTURE, dict(value, key="unenrolled")).status_code, 409)
        self.db.execute(
            "CREATE TRIGGER refuse_sources BEFORE INSERT ON integration_lineage_sources BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
        reply = call(CAPTURE, value)
        self.assertEqual(reply.status_code, 503)
        for table in (
            "recovery_plans",
            "integration_lineage_resources",
            "integration_lineage_sources",
        ):
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM " + table).fetchone(), (0,)
            )
        self.db.execute("DROP TRIGGER refuse_sources")
        self.assertEqual(call(CAPTURE, value).json, {"stored": True})
        self.assertEqual(call(CAPTURE, dict(value, body="changed")).status_code, 409)
        self.assertEqual(
            call(READ, read, grant=reader).json, {"body": "private", "binding": admission},
        )
        # A damaged retained source edge must not let an authorized reader expose the body.
        self.db.execute("UPDATE integration_lineage_sources SET store='18'")
        self.assertEqual(call(READ, read, grant=reader).status_code, 409)
        self.db.execute("UPDATE integration_lineage_sources SET store='17'")
        request_id = str(uuid4())
        self.op.start(request_id, "subject", S)
        held = call(READ, read, grant=reader)
        self.assertEqual(held.status_code, 409)
        self.assertNotIn(b"private", held.data)
        self.assertEqual(held.headers["Cache-Control"], "no-store")
        self.assertTrue(self.op.drain(request_id)["registered_scope_complete"])
        self.assertEqual(call(CAPTURE, value).status_code, 409)
        self.assertEqual(call(READ, read, grant=reader).status_code, 409)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM recovery_plans").fetchone(), (0,)
        )


if __name__ == "__main__":
    unittest.main()
