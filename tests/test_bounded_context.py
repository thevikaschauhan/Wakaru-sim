import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from flask import Flask
from cart_recovery.bounded_context_api import create_blueprint
from cart_recovery.integration_privacy import PrivacyStore, PrivacyStop
from cart_recovery.intelligence.bounded_context import BoundedService, interpret, digest, read_input
from cart_recovery.intelligence.integration_contracts import context_hash

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    request = json.loads((ROOT / "contracts/integrations/c8/fixture.json").read_text())["request"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    c = request["context"]
    customer = c["customer_context"]
    c["as_of"] = customer["as_of"] = (now-timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    customer["expires_at"] = (now+timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
    customer["coverage"][0]["verified_at"] = c["as_of"]
    for statement in request["statements"]:
        statement["expires_at"] = customer["expires_at"]
        statement["reference"]["observed_at"] = statement["reference"]["source_updated_at"] = c["as_of"]
    customer["evidence"] = [s["reference"] for s in request["statements"]]
    customer["snapshot_hash"] = context_hash(customer, "snapshot_hash")
    c["context_hash"] = context_hash(c, "context_hash")
    bound = dict(merchant=c["merchant_id"], store=customer["store_id"], subject=customer["subject_id"], episode=c["episode_id"], now=now)
    return request, bound


class BoundedTests(unittest.TestCase):
    def test_published_fixture_matches_interpreter(self):
        v = json.loads((ROOT / "contracts/integrations/c8/fixture.json").read_text())
        c = v["request"]["context"]
        cc = c["customer_context"]
        got = interpret(json.dumps(v["request"]), merchant=c["merchant_id"], store=cc["store_id"], subject=cc["subject_id"], episode=c["episode_id"], now=datetime(2026, 10, 21, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(got, v["plan"])

    def test_copy_is_only_approved_facts_and_never_current_motive(self):
        value, bound = fixture()
        plan = interpret(json.dumps(value), **bound)
        self.assertEqual(plan["allowed_claims"], [value["context"]["approved_facts"][0]["text"]])
        self.assertEqual(plan["action"], "review")
        self.assertEqual(plan["status"], "needs_review")
        self.assertNotIn("next-day", json.dumps(plan))
        self.assertEqual(plan["revision"], context_hash(plan, "revision"))
        for version in ("recovery_context_v1", "unknown"):
            changed = copy.deepcopy(value)
            changed["context"]["version"] = version
            with self.assertRaises(Exception):
                interpret(json.dumps(changed), **bound)

    def test_historical_order_and_unrelated_product_do_not_become_motives(self):
        for scope, scope_id in (("order", "past-order"), ("product", "unrelated")):
            value, bound = fixture()
            ref = value["statements"][0]["reference"]
            ref.update(scope=scope, scope_id=scope_id)
            c = value["context"]
            c["customer_context"]["snapshot_hash"] = context_hash(c["customer_context"], "snapshot_hash")
            c["context_hash"] = context_hash(c, "context_hash")
            plan = interpret(json.dumps(value), **bound)
            self.assertEqual(plan["allowed_claims"], [])
            self.assertEqual(plan["source_refs"], [])

    def test_untrusted_selector_cannot_expand_authority(self):
        value, bound = fixture()
        for choices in ([('invented', digest(value['statements'][0]['reference']))], [('shipping-fact', 'invented')]):
            with self.assertRaises(ValueError):
                interpret(json.dumps(value), **bound, selector=lambda *_: choices)
        for mutation in (lambda v: v['statements'][0].update(visibility='private'), lambda v: v['statements'][0].update(author='agent'), lambda v: v['statements'][0].update(text='tampered')):
            changed = copy.deepcopy(value)
            mutation(changed)
            with self.assertRaises(ValueError):
                interpret(json.dumps(changed), **bound)

    def test_http_sync_async_privacy_erasure_and_late_worker(self):
        value, bound = fixture()
        with tempfile.TemporaryDirectory() as directory:
            store = PrivacyStore(Path(directory)/'privacy.sqlite')
            service = BoundedService(store)
            app = Flask(__name__)
            app.register_blueprint(create_blueprint(service, lambda req: req.headers.get('X-Fixture-Merchant')), url_prefix='/context')
            client = app.test_client()
            headers = {'X-Fixture-Merchant': bound['merchant']}
            self.assertEqual(client.post('/context/analyze', json=value).status_code, 401)
            self.assertEqual(client.post('/context/analyze', json=value, headers={'X-Fixture-Merchant':'foreign'}).status_code, 422)
            sync = client.post('/context/analyze', json=value, headers=headers)
            self.assertEqual(sync.status_code, 200)
            queued = client.post('/context/jobs', json=value, headers=headers)
            self.assertEqual(queued.status_code, 202)
            job = queued.json['job_id']
            service.selector = lambda *_: self.fail("frozen replay invoked selector")
            plan = service.run(bound['merchant'], bound['store'], job, now=bound['now'])
            self.assertEqual(plan, sync.json['plan'])
            self.assertEqual(client.get(f'/context/jobs/17/{job}', headers=headers).json['plan'], plan)
            self.assertEqual(client.get(f'/context/jobs/17/{job}', headers={'X-Fixture-Merchant':'foreign'}).status_code, 404)
            task = dict(version='integration_privacy_task_v1', merchant_id=bound['merchant'], store_id='17', request_id='erase', subject_key=hashlib.sha256(bound['subject'].encode()).hexdigest(), privacy_generation=2, operation='erase', scope='subject', source_keys=value['source_keys'], inventory_revision='integration_c5_v1', live_ingestion_enabled=False, email_use_enabled=False)
            store.apply(bound['merchant'], '17', json.dumps(task), now=bound['now'].timestamp())
            self.assertEqual(client.get(f'/context/jobs/17/{job}', headers=headers).status_code, 404)
            with self.assertRaises(PrivacyStop):
                service.run(bound['merchant'], '17', job, now=bound['now'])
            self.assertEqual(client.post('/context/analyze', json=value, headers=headers).status_code, 422)
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM privacy_artifacts').fetchone()[0], 0)

    def test_erasure_during_selection_rejects_result(self):
        value, bound = fixture()
        with tempfile.TemporaryDirectory() as directory:
            store = PrivacyStore(Path(directory)/'privacy.sqlite')
            def selector(*_):
                with store.transaction() as db:
                    db.execute('UPDATE privacy_scopes SET erased=1')
                return []
            service = BoundedService(store, selector)
            with self.assertRaises(PrivacyStop):
                service.analyze(json.dumps(value), **bound)
            with store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM privacy_artifacts WHERE kind='plan'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
