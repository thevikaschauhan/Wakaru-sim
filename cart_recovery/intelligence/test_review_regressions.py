"""Provider accounting, inactive-tenant retention, and contract pin regressions."""

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from .recovery import PlanJournal
from .test_typesafe import CONTRACTS
from .typesafe import Client, Store


@pytest.mark.parametrize(
    "field,value",
    [
        ("probabilities", ["blue", "red"]),
        ("probabilities", None),
        ("probabilities", 1),
        ("usage", None),
        ("usage", []),
        ("usage", "bad"),
    ],
)
def test_malformed_provider_response_is_durably_recorded(tmp_path, field, value):
    fixture = json.loads((CONTRACTS / "conformance.json").read_text())[0]
    response = copy.deepcopy(fixture["response"])
    if field == "usage":
        response[field] = value
    else:
        response["answers"]["color"][field] = value
    client = Client(
        key="fixture",
        approved_models=[fixture["response"]["model"]],
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    try:
        store = Store(tmp_path / "journal.db", client)
        identity = dict(
            merchant_id="m",
            workflow="draft_claims",
            revision="1",
            snapshot_hash="h",
            rubric_hash="r",
            policy_version="v",
            candidate_hash="c",
            as_of="2026-09-23",
            episode_id="e",
        )
        result = store.judge(identity, fixture["request"])["result"]
        assert (
            result["status"] == "unresolved"
            and result["error_class"] == "schema_failure"
        )
        assert len(result["attempts"]) == 1
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM results").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM judgments").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM selected").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
    finally:
        client.close()


def test_active_request_expires_inactive_merchant_journal(tmp_path):
    fixture = json.loads((CONTRACTS / "conformance.json").read_text())[0]
    client = Client(
        key="fixture",
        approved_models=[fixture["response"]["model"]],
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=fixture["response"])
        ),
    )
    try:
        store = Store(tmp_path / "journal.db", client)
        now = time.time()
        with store.connection() as db:
            for merchant, created in [("inactive", now - 91 * 86400), ("recent", now)]:
                db.execute(
                    "INSERT INTO attempts VALUES(?,?,?,?,?)",
                    (merchant, merchant, "shared", created, 0.001),
                )
                db.execute(
                    "INSERT INTO results VALUES(?,?,?)", (merchant, merchant, "{}")
                )
                db.execute(
                    "INSERT INTO raw_outputs VALUES(?,?,?,?)",
                    (merchant, merchant, "private", created),
                )
                db.execute(
                    "INSERT INTO selected VALUES(?,?,?)", (merchant, "shared", "{}")
                )
                db.execute(
                    "INSERT INTO judgments VALUES(?,?,?,?,?)",
                    (merchant, merchant, "shared", "{}", created),
                )
            db.execute(
                "INSERT INTO raw_outputs VALUES(?,?,?,?)",
                ("week-old", "inactive", "private", now - 8 * 86400),
            )
        identity = dict(
            merchant_id="active",
            workflow="draft_claims",
            revision="1",
            snapshot_hash="h",
            rubric_hash="r",
            policy_version="v",
            candidate_hash="c",
            as_of="2026-09-23",
            episode_id="e",
        )
        store.judge(identity, fixture["request"])
        with store.connection() as db:
            for table in (
                "attempts",
                "results",
                "raw_outputs",
                "selected",
                "judgments",
            ):
                assert (
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE merchant='inactive'"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE merchant='recent'"
                    ).fetchone()[0]
                    == 1
                )
    finally:
        client.close()


def test_plan_load_expires_inactive_merchants_without_cross_tenant_reads(tmp_path):
    journal = PlanJournal(tmp_path / "plans.db")
    now = time.time()
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,?)",
            ("inactive", "old", "{}", now - 8 * 86400),
        )
        db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,?)",
            ("recent", "same", '{"private":true}', now),
        )
    assert journal.load("active", "same") is None
    with sqlite3.connect(journal.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM recovery_plans WHERE merchant='inactive'"
            ).fetchone()[0]
            == 0
        )
    assert journal.load("recent", "same") == {"private": True}


def test_storage_contract_drift_is_detected(tmp_path):
    target = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, target)
    manifest = json.loads((target / "manifest.json").read_text())
    assert (
        manifest["sha256"].get("storage.sql")
        == hashlib.sha256((target / "storage.sql").read_bytes()).hexdigest()
    )
    with (target / "storage.sql").open("a") as stream:
        stream.write("\n-- drift\n")
    result = subprocess.run(
        [sys.executable, str(target / "verify.py")], capture_output=True, text=True
    )
    assert result.returncode != 0 and "storage.sql" in result.stderr


def test_idle_retention_retries_then_purges_without_provider_work(
    tmp_path, monkeypatch, caplog
):
    """An idle, disabled worker still expires plans and retries SQLite failures."""
    import logging
    import threading
    from . import maintenance

    path = tmp_path / "idle.db"
    journal = PlanJournal(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,?)",
            ("inactive", "old", "{}", time.time() - 8 * 86400),
        )
        db.execute(
            "INSERT INTO recovery_plans VALUES(?,?,?,?)",
            ("recent", "new", "{}", time.time()),
        )
    monkeypatch.setenv("TYPESAFE_JOURNAL_PATH", str(path))
    monkeypatch.setenv("TYPESAFE_RECOVERY_MODE", "off")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    finished = threading.Event()
    calls = []
    purge = maintenance.purge_journal

    def flaky_purge(path):
        calls.append(path)
        if len(calls) == 1:
            raise sqlite3.OperationalError("locked")
        purge(path)
        finished.set()

    monkeypatch.setattr(maintenance, "purge_journal", flaky_purge)
    with caplog.at_level(logging.ERROR, logger="typesafe.retention"):
        stop = maintenance.start_retention_monitor(interval=0.01)
        try:
            assert finished.wait(2), "idle cleanup did not retry"
        finally:
            stop.set()
    assert "retention failed" in caplog.text
    assert journal.load("recent", "new") == {}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM recovery_plans").fetchone()[0] == 1


def test_global_retention_preserves_current_budgets_and_leases(tmp_path):
    """Global maintenance removes old metadata without resetting today's caps."""
    from .maintenance import purge_journal

    store = Store(tmp_path / "journal.db", None)
    now = time.time()
    with store.connection() as db:
        for key, day in [
            ("old", "2000-01-01"),
            ("today", time.strftime("%Y-%m-%d", time.gmtime())),
        ]:
            db.execute("INSERT INTO budgets VALUES(?,?,?)", (key, day, 0.25))
        db.execute("INSERT INTO leases VALUES(?,?,?)", ("expired", "dead", now - 1))
        db.execute("INSERT INTO leases VALUES(?,?,?)", ("active", "live", now + 60))
    purge_journal(store.path)
    with store.connection() as db:
        assert db.execute("SELECT key,spent FROM budgets").fetchall() == [
            ("today", 0.25)
        ]
        assert db.execute("SELECT merchant,owner FROM leases").fetchall() == [
            ("active", "live")
        ]
    absent = tmp_path / "never-created.db"
    purge_journal(absent)
    assert not absent.exists()
