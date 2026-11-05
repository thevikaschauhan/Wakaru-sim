"""Local journal retention, independent of merchant activity or model access."""

from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

logger = logging.getLogger("typesafe.retention")
RETENTION_INTERVAL_SECONDS = 3600


def has_lineage_ledger(db):
    return db.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='integration_lineage_resources'"
    ).fetchone() is not None


def purge_unregistered(db, table, due, params):
    """Caller holds BEGIN IMMEDIATE. Leave adopted data for an accountable operator."""
    kind, key = {"recovery_plans": ("plan", "revision"),
                 "raw_outputs": ("raw_output", "id"),
                 "attempts": ("raw_output", "id")}[table]
    if not has_lineage_ledger(db):
        db.execute(f"DELETE FROM main.{table} WHERE {due}", params)
        return 0
    registered = (
        "EXISTS (SELECT 1 FROM main.integration_lineage_resources AS r "
        f"WHERE r.merchant={table}.merchant AND r.kind=? AND r.key={table}.{key})"
    )
    args = (*params, kind)
    deferred, = db.execute(
        f"SELECT count(*) FROM main.{table} WHERE ({due}) AND {registered}", args
    ).fetchone()
    db.execute(f"DELETE FROM main.{table} WHERE ({due}) AND NOT {registered}", args)
    if deferred:
        logger.warning("%s expired %s rows deferred to the lineage operator", deferred, table)
    return deferred


def purge_journal(path):
    """Purge expired rows globally without creating a journal or calling a model.

    Raw responses and recovery plans live seven days; audit metadata lives
    ninety. Recent rows remain tenant-scoped, even when request keys collide.
    A missing file is normal before the first judgment. Other failures surface
    to the caller so retention failures cannot be mistaken for success.
    """
    path = Path(path).resolve()
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
    except sqlite3.OperationalError:
        if not path.exists():
            return 0
        raise
    now = time.time()
    raw_cutoff, audit_cutoff = now - 7 * 86400, now - 90 * 86400
    with closing(connection) as db, db:
        db.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        deferred = 0
        if "raw_outputs" in tables:
            due, params = "created<?", (raw_cutoff,)
            if "attempts" in tables:
                due += """ OR EXISTS (SELECT 1 FROM main.attempts a
                    WHERE a.merchant=raw_outputs.merchant AND a.id=raw_outputs.id AND a.started<?)"""
                params += (audit_cutoff,)
            deferred += purge_unregistered(db, "raw_outputs", due, params)
        if "judgments" in tables:
            db.execute("DELETE FROM judgments WHERE created<?", (audit_cutoff,))
        if "attempts" in tables:
            if "selected" in tables:
                db.execute(
                    """DELETE FROM selected WHERE EXISTS (
                    SELECT 1 FROM attempts a WHERE a.merchant=selected.merchant
                    AND a.request_key=selected.request_key AND a.started<?)""",
                    (audit_cutoff,),
                )
            if "results" in tables:
                db.execute(
                    """DELETE FROM results WHERE EXISTS (
                    SELECT 1 FROM attempts a WHERE a.merchant=results.merchant
                    AND a.id=results.id AND a.started<?)""", (audit_cutoff,)
                )
            deferred += purge_unregistered(db, "attempts", "started<?", (audit_cutoff,))
        if "budgets" in tables:
            day_cutoff = time.strftime("%Y-%m-%d", time.gmtime(audit_cutoff))
            db.execute("DELETE FROM budgets WHERE day<?", (day_cutoff,))
        if "leases" in tables:
            db.execute("DELETE FROM leases WHERE expires<?", (now,))
        if "recovery_plans" in tables:
            deferred += purge_unregistered(db, "recovery_plans", "created<?", (raw_cutoff,))
    return deferred


def start_retention_monitor(interval=RETENTION_INTERVAL_SECONDS):
    """Start hourly cleanup on the worker's own volume, including idle periods.

    Each pass opens and closes its own SQLite connection without sharing one
    with request handlers. The returned event stops the daemon at shutdown.
    """
    if interval <= 0:
        raise ValueError("invalid retention interval")
    stop = threading.Event()
    path = os.getenv("TYPESAFE_JOURNAL_PATH", "")
    if not path or not os.path.isabs(path):
        return stop

    def monitor():
        """Retry failures on the next interval and emit content-free status."""
        while not stop.is_set():
            try:
                deferred = purge_journal(path)
            except Exception:
                logger.error("TypeSafe journal retention failed; retry scheduled")
            else:
                if not deferred:
                    logger.info("TypeSafe journal retention completed")
            if stop.wait(interval):
                return

    threading.Thread(target=monitor, name="typesafe-retention", daemon=True).start()
    return stop
