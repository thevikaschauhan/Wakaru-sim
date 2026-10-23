"""Dormant C5 store for integration-derived journals/results and frozen inputs.

C8 must adopt these writes before consuming integration context. This inventory
excludes legacy journals, scratch graphs, external files and vendor copies.
There is no provider transport, scheduler, public route or send authorization.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .intelligence.integration_contracts import read_contract

KEY = re.compile(r"^[a-f0-9]{64}$")
KINDS = {"input_snapshot", "journal", "result", "plan", "judgment", "pending_job"}


class PrivacyStop(ValueError):
    pass


class PrivacyStore:
    def __init__(self, path):
        self.path = str(Path(path))
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS privacy_scopes(merchant TEXT,store TEXT,generation INTEGER NOT NULL DEFAULT 1,erased INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(merchant,store));
            CREATE TABLE IF NOT EXISTS privacy_tombstones(merchant TEXT,store TEXT,kind TEXT,key TEXT,PRIMARY KEY(merchant,store,kind,key));
            CREATE TABLE IF NOT EXISTS privacy_artifacts(merchant TEXT,store TEXT,id TEXT,subject TEXT,generation INTEGER,kind TEXT,sources TEXT,payload TEXT,expires REAL,PRIMARY KEY(merchant,store,id));
            CREATE INDEX IF NOT EXISTS privacy_artifacts_expiry ON privacy_artifacts(merchant,store,expires);
            CREATE TABLE IF NOT EXISTS privacy_requests(merchant TEXT,store TEXT,id TEXT,fingerprint TEXT,task TEXT,complete INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(merchant,id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def lock(db, merchant, store):
        if not merchant or not store:
            raise PrivacyStop("invalid_binding")
        db.execute("INSERT OR IGNORE INTO privacy_scopes(merchant,store)VALUES(?,?)", (merchant, store))
        return db.execute("SELECT generation,erased FROM privacy_scopes WHERE merchant=? AND store=?", (merchant, store)).fetchone()

    @staticmethod
    def blocked(db, merchant, store, subject, sources):
        return any(kind == "subject" and key == subject or kind == "source" and key in sources
                   for kind, key in db.execute("SELECT kind,key FROM privacy_tombstones WHERE merchant=? AND store=?", (merchant, store)))

    def put(self, merchant, store, *, artifact_id, subject, generation, kind, sources, payload, expires, now):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if kind not in KINDS or not KEY.fullmatch(subject) or not artifact_id or len(artifact_id) > 256 or not 0 < len(sources) <= 100 or not all(KEY.fullmatch(k) for k in sources) or len(encoded.encode()) > 200000 or not 0 < expires-now <= 90*86400:
            raise PrivacyStop("invalid_artifact")
        with self.transaction() as db:
            current, erased = self.lock(db, merchant, store)
            if erased or current != generation or self.blocked(db, merchant, store, subject, sources):
                raise PrivacyStop("privacy_stop")
            value = (subject, generation, kind, json.dumps(sources), encoded, expires)
            old = db.execute("SELECT subject,generation,kind,sources,payload,expires FROM privacy_artifacts WHERE merchant=? AND store=? AND id=?", (merchant, store, artifact_id)).fetchone()
            if old is not None:
                if old != value:
                    raise PrivacyStop("artifact_conflict")
                return
            db.execute("INSERT INTO privacy_artifacts VALUES(?,?,?,?,?,?,?,?,?)", (merchant, store, artifact_id, *value))

    def read(self, merchant, store, artifact_id, *, now):
        with self.transaction() as db:
            current, erased = self.lock(db, merchant, store)
            row = db.execute("SELECT subject,generation,sources,payload,expires FROM privacy_artifacts WHERE merchant=? AND store=? AND id=?", (merchant, store, artifact_id)).fetchone()
            if erased or row is None or row[1] != current or row[4] <= now or self.blocked(db, merchant, store, row[0], json.loads(row[2])):
                raise PrivacyStop("privacy_stop")
            return json.loads(row[3])

    def apply(self, merchant, store, raw, *, now):
        task = read_contract(raw, merchant=merchant, store=store)
        if task["version"] != "integration_privacy_task_v1" or task["scope"] == "subject" and not task["source_keys"]:
            raise PrivacyStop("invalid_task")
        encoded = json.dumps(task, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction() as db:
            current, _ = self.lock(db, merchant, store)
            old = db.execute("SELECT fingerprint FROM privacy_requests WHERE merchant=? AND id=?", (merchant, task["request_id"])).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise PrivacyStop("request_conflict")
            else:
                if task["operation"] == "export" and current != task["privacy_generation"]:
                    raise PrivacyStop("privacy_stop")
                db.execute("INSERT INTO privacy_requests VALUES(?,?,?,?,?,0)", (merchant, store, task["request_id"], fingerprint, encoded))
                if task["operation"] != "export":
                    db.execute("UPDATE privacy_scopes SET generation=max(generation,?),erased=erased OR ? WHERE merchant=? AND store=?", (task["privacy_generation"], task["scope"] == "store" and task["operation"] == "erase", merchant, store))
                    if task["operation"] == "erase":
                        db.execute("INSERT OR IGNORE INTO privacy_tombstones VALUES(?,?,'subject',?)", (merchant, store, task["subject_key"]))
                    for key in task["source_keys"]:
                        db.execute("INSERT OR IGNORE INTO privacy_tombstones VALUES(?,?,'source',?)", (merchant, store, key))
        # Stop is committed before destructive work. Failures remain retryable.
        output = []
        with self.transaction() as db:
            current, erased = self.lock(db, merchant, store)
            if task["operation"] == "export" and (erased or current != task["privacy_generation"] or self.blocked(db, merchant, store, task["subject_key"], task["source_keys"])):
                raise PrivacyStop("privacy_stop")
            if task['operation'] == 'export':
                rows = db.execute("""SELECT payload FROM privacy_artifacts a WHERE merchant=? AND store=?
                    AND (? OR subject=? OR EXISTS(SELECT 1 FROM json_each(a.sources) s WHERE s.value IN (SELECT value FROM json_each(?))))
                    AND expires>? AND NOT EXISTS(SELECT 1 FROM privacy_tombstones t WHERE t.merchant=a.merchant AND t.store=a.store AND
                      ((t.kind='subject' AND t.key=a.subject) OR (t.kind='source' AND t.key IN (SELECT value FROM json_each(a.sources))))) ORDER BY id LIMIT 10001""",
                    (merchant,store,task['scope']=='store',task['subject_key'],json.dumps(task['source_keys']),now))
                size=0
                for (payload,) in rows:
                    size+=len(payload.encode())
                    if len(output)>=10000 or size>20000000:
                        raise PrivacyStop('export_size_limit')
                    output.append(json.loads(payload))
            else:
                db.execute("""DELETE FROM privacy_artifacts WHERE merchant=? AND store=? AND
                    (? OR (? AND subject=?) OR EXISTS(SELECT 1 FROM json_each(privacy_artifacts.sources) WHERE value IN (SELECT value FROM json_each(?))) OR (kind='pending_job' AND generation<?))""",
                    (merchant,store,task['scope']=='store' and task['operation']=='erase',task['operation']=='erase',task['subject_key'],json.dumps(task['source_keys']),current))
            db.execute("UPDATE privacy_requests SET complete=1 WHERE merchant=? AND id=?", (merchant, task["request_id"]))
        receipt = {k: task[k] for k in ("merchant_id", "store_id", "request_id", "subject_key", "privacy_generation", "operation", "scope", "inventory_revision")}
        receipt.update(version="integration_privacy_receipt_v1", service="wakaru", status="complete")
        return receipt, output

    def purge_expired(self, merchant, store, *, now):
        """Retire revision keys and their local derivatives; no scheduler runs."""
        import time
        if not isinstance(now, (int, float)) or not 0 < now <= time.time()+1:
            raise PrivacyStop('invalid_retention_time')
        with self.transaction() as db:
            self.lock(db, merchant, store)
            keys=[key for (key,) in db.execute("SELECT DISTINCT value FROM privacy_artifacts,json_each(sources) WHERE merchant=? AND store=? AND expires<=? ORDER BY value LIMIT 2000", (merchant,store,now))]
            for key in keys:
                db.execute("INSERT OR IGNORE INTO privacy_tombstones VALUES(?,?,'source',?)", (merchant, store, key))
            result=db.execute("DELETE FROM privacy_artifacts WHERE merchant=? AND store=? AND EXISTS(SELECT 1 FROM json_each(privacy_artifacts.sources) WHERE value IN (SELECT value FROM json_each(?)))",(merchant,store,json.dumps(keys)))
            return result.rowcount
