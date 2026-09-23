"""Immutable artifact registry with explicit reviewed promotion and revocation."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

from .data import TARGET, canonical, digest, moment, vector
from .model import predict
from .training import POLICY


class Registry:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, artifact TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS promotions(id TEXT PRIMARY KEY REFERENCES models(id), reviewer TEXT NOT NULL, review_ref TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS erased_merchants(merchant_id TEXT PRIMARY KEY, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revocations(id TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_model BEFORE UPDATE ON models BEGIN SELECT RAISE(ABORT,'immutable model'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_promotion BEFORE UPDATE ON promotions BEGIN SELECT RAISE(ABORT,'immutable promotion'); END;
            """)

    @classmethod
    def readonly(cls, path):
        result = object.__new__(cls)
        result.path = str(Path(path).resolve())
        return result

    def register(self, artifact):
        body = {k: v for k, v in artifact.items() if k != "model_id"}
        if (
            artifact["model_id"] != digest(body)
            or artifact["promotion_policy"] != POLICY
            or artifact["target"] != TARGET
        ):
            raise ValueError("invalid_artifact")
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            if any(
                db.execute(
                    "SELECT 1 FROM erased_merchants WHERE merchant_id=?",
                    (d["merchant_id"],),
                ).fetchone()
                for d in artifact["datasets"]
            ):
                raise ValueError("training_tenant_erased")
            db.execute(
                "INSERT OR IGNORE INTO models VALUES(?,?)",
                (artifact["model_id"], canonical(artifact).decode()),
            )
        return artifact["model_id"]

    def artifact(self, model_id):
        with sqlite3.connect(self.path) as db:
            result = db.execute(
                "SELECT artifact FROM models WHERE id=? AND NOT EXISTS(SELECT 1 FROM revocations WHERE id=?)",
                (model_id, model_id),
            ).fetchone()
        return json.loads(result[0]) if result else None

    def promote(self, model_id, reviewer, review_ref):
        if (
            not reviewer.strip()
            or not review_ref.strip()
            or max(len(reviewer), len(review_ref)) > 500
        ):
            raise ValueError("review_provenance_required")
        # Atomic with deletion/revocation. No stale read can resurrect a model.
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(
                "SELECT artifact FROM models WHERE id=? AND NOT EXISTS(SELECT 1 FROM revocations WHERE id=?)",
                (model_id, model_id),
            ).fetchone()
            artifact = json.loads(found[0]) if found else None
            if (
                not artifact
                or artifact["origin"] != "real"
                or not artifact["promotion_eligible"]
                or not artifact["gates"]
                or not all(artifact["gates"].values())
                or artifact["promotion_policy"] != POLICY
            ):
                raise ValueError("promotion_gates_failed")
            db.execute(
                "INSERT INTO promotions VALUES(?,?,?,?)",
                (
                    model_id,
                    reviewer,
                    review_ref,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def delete_merchant(self, merchant_id):
        """Erase pooled weights and reports containing this tenant, revoke IDs.

        Original private export files have their own operator retention policy.
        """
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO erased_merchants VALUES(?,?)",
                (merchant_id, datetime.now(timezone.utc).isoformat()),
            )
            ids = [
                key
                for key, raw in db.execute("SELECT id,artifact FROM models")
                if any(
                    d["merchant_id"] == merchant_id for d in json.loads(raw)["datasets"]
                )
            ]
            for key in ids:
                db.execute(
                    "INSERT OR IGNORE INTO revocations VALUES(?,?,?)",
                    (
                        key,
                        "training_data_deleted",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.execute("DELETE FROM promotions WHERE id=?", (key,))
                db.execute("DELETE FROM models WHERE id=?", (key,))
        return len(ids)

    def estimate(self, model_id, features, as_of):
        result = {
            "version": "conversion_estimate_v1",
            "status": "not_available",
            "probability": None,
            "target": TARGET,
            "horizon_seconds": 604800,
            "model_version": model_id or None,
        }
        with sqlite3.connect(
            Path(self.path).resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            db.execute("BEGIN")
            found = db.execute(
                "SELECT artifact,EXISTS(SELECT 1 FROM promotions WHERE id=?) FROM models WHERE id=? AND NOT EXISTS(SELECT 1 FROM revocations WHERE id=?)",
                (model_id, model_id, model_id),
            ).fetchone()
        if not found:
            return result
        artifact, promoted = json.loads(found[0]), found[1]
        if artifact["status"] == "insufficient_data":
            return {**result, "status": "insufficient_data"}
        if not promoted:
            return result
        try:
            x, at = vector(features), moment(as_of)
            newest = max(moment(d["as_of"]) for d in artifact["datasets"])
            trained = moment(artifact["trained_at"])
            model = artifact["models"]["event_and_semantic"]
            if (
                at < max(newest, trained)
                or at > trained + timedelta(days=POLICY["max_age_days"])
                or any(
                    v < low or v > high for v, (low, high) in zip(x, model["ranges"])
                )
            ):
                raise ValueError("out_of_distribution")
        except (ValueError, KeyError, TypeError):
            return {**result, "status": "out_of_distribution"}
        return {**result, "status": "available", "probability": predict(model, [x])[0]}
