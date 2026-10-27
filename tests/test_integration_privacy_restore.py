"""C14 real SQLite backups, ledger-first quarantine and resumed erasure."""
import hashlib
import json
import sqlite3
import time

import pytest

from cart_recovery.integration_privacy import PrivacyStore, PrivacyStop
from cart_recovery.integration_invalidation import VERSION


def test_c14_sqlite_backup_ledger_restore_and_replay(tmp_path):
    path = tmp_path / "source.sqlite"
    store = PrivacyStore(path)
    now = time.time()
    merchant, tenant, scope = "10000000-0000-4000-8000-000000000001", "20000000-0000-4000-8000-000000000002", "17"
    subject, source = hashlib.sha256(b"subject-42").hexdigest(), "b" * 64
    for owner in (merchant, tenant):
        for kind in sorted(("input_snapshot", "journal", "result", "plan", "judgment", "pending_job")):
            store.put(owner, scope, artifact_id=kind, subject=subject, generation=1,
                      kind=kind, sources=[source], payload={"private": "synthetic"},
                      expires=now + 3600, now=now)
    old_path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as live, sqlite3.connect(old_path) as backup:
        live.backup(backup)
    task = dict(version="integration_privacy_task_v1", merchant_id=merchant, store_id=scope,
                request_id="c14-erase", subject_key=subject, privacy_generation=2,
                operation="erase", scope="subject", source_keys=[source],
                inventory_revision="integration_c5_v1", live_ingestion_enabled=False, email_use_enabled=False)
    raw = json.dumps(task)
    receipt, _ = store.apply(merchant, scope, raw, now=now)
    event = dict(version=VERSION, merchant_id=merchant, store_id=scope, epoch=7, cutoff_us=int(now * 1000000))
    store.apply_invalidation(merchant, scope, event)
    ledger_path = tmp_path / "retained-ledger.sqlite"
    with sqlite3.connect(path) as live, sqlite3.connect(ledger_path) as backup:
        live.backup(backup)
    restored_path = tmp_path / "quarantined.sqlite"
    with sqlite3.connect(old_path) as backup, sqlite3.connect(restored_path) as restored:
        backup.backup(restored)
    # No service is constructed against this file until the independent current
    # ledger is verified and installed. Atomic transaction, never overwrite it
    # with the old backup's generation or request completion.
    tables = ("privacy_scopes", "privacy_tombstones", "privacy_requests", "invalidation_scopes")
    with sqlite3.connect(ledger_path) as ledger, sqlite3.connect(restored_path) as restored:
        retained = {table: ledger.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        assert retained["privacy_tombstones"] and retained["invalidation_scopes"]
        for table in tables:
            restored.execute(f"DELETE FROM {table}")
            for row in retained[table]:
                restored.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)
    restarted = PrivacyStore(restored_path)
    with pytest.raises(PrivacyStop):
        restarted.read(merchant, scope, "result", now=now)
    with sqlite3.connect(restored_path) as db:
        assert db.execute("SELECT count(*) FROM privacy_artifacts WHERE merchant=?", (merchant,)).fetchone()[0] == 6
    assert restarted.apply(merchant, scope, raw, now=now)[0] == receipt
    assert restarted.apply(merchant, scope, raw, now=now)[0] == receipt
    with sqlite3.connect(restored_path) as db:
        assert db.execute("SELECT count(*) FROM privacy_artifacts WHERE merchant=?", (merchant,)).fetchone()[0] == 0
        assert db.execute("SELECT epoch FROM invalidation_scopes WHERE merchant=?", (merchant,)).fetchone()[0] == 7
    assert restarted.read(tenant, scope, "result", now=now) == {"private": "synthetic"}
    with pytest.raises(PrivacyStop):
        restarted.put(merchant, scope, artifact_id="late", subject=subject, generation=2,
                      kind="result", sources=[source], payload={}, expires=now + 3600, now=now)
    with pytest.raises(PrivacyStop):
        restarted.context_current(sqlite3.connect(restored_path), merchant, scope, "2020-01-01T00:00:00Z")
