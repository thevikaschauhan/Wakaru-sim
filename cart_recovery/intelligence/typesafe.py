"""OpenRouter Decisions adapter and durable SQLite journal for Wakaru workers.

Use a shared persistent volume, as with Wakaru's existing workflow artifacts.
No browser keys, payload logging, or retry-to-improve-confidence behavior.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path

import httpx
from .operations import control
from .telemetry import observe_judgment

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
ADAPTER_VERSION = "openrouter-v1"
RESERVATION = 0.000625


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def probability(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def validate(request, response):
    """Reject malformed provider fields before recording an evaluated result."""
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not response["model"]:
        raise ValueError("invalid_model")
    answers = response.get("answers")
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise ValueError("invalid_usage")
    if "cost" in usage:
        cost = usage["cost"]
        try:
            valid_cost = (
                isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(cost)
                and cost >= 0
            )
        except OverflowError:
            valid_cost = False
        if not valid_cost:
            raise ValueError("invalid_usage")
    if not isinstance(answers, dict) or set(answers) != set(request["questions"]):
        raise ValueError("invalid_answers")
    for key, question in request["questions"].items():
        answer = answers[key]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError("invalid_type")
        if kind == "noul":
            if set(answer) != {"type", "noul"} or not probability(answer["noul"]):
                raise ValueError("invalid_noul")
            continue
        if not probability(answer.get("confidence")):
            raise ValueError("invalid_confidence")
        criteria = question["criteria"]
        keys = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
        dist = answer.get("probabilities", {})
        if not isinstance(dist, dict) or set(dist) != keys or not all(probability(x) for x in dist.values()) or abs(sum(dist.values()) - 1) > 0.015:
            raise ValueError("invalid_distribution")
        if kind == "choice":
            if set(answer) != {"type", "choice", "confidence", "probabilities"} or answer.get("choice") not in keys or dist[answer["choice"]] < max(dist.values()):
                raise ValueError("illegal_choice")
        elif kind == "score":
            score = answer.get("score")
            if set(answer) != {"type", "score", "confidence", "probabilities", "legend"} or set(answer.get("legend", {})) != keys or not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or abs(score - sum(int(k)*v for k,v in dist.items())) > 0.03:
                raise ValueError("invalid_score")
        else:
            raise ValueError("invalid_type")


class Client:
    def __init__(self, key=None, model=None, approved_models=None, transport=None, deadline=3, base_url=ENDPOINT):
        if base_url != ENDPOINT or not 0 < deadline <= 3:
            raise ValueError("invalid_provider_configuration")
        self.key = key if key is not None else os.getenv("OPENROUTER_API_KEY", "")
        self.model = model or os.getenv("TYPESAFE_MODEL", "typesafe/jev-1.13")
        self.approved = approved_models if approved_models is not None else os.getenv("TYPESAFE_APPROVED_MODELS", "").split(",")
        self.deadline = deadline
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="typesafe")
        self.slots = threading.BoundedSemaphore(8)
        self.http = httpx.Client(transport=transport, follow_redirects=False, limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.http.close()

    def _request(self, encoded, remaining):
        try:
            with self.http.stream("POST", ENDPOINT, content=encoded, headers={"Authorization":"Bearer "+self.key,"Content-Type":"application/json"}, timeout=remaining) as response:
                body=bytearray()
                for chunk in response.iter_bytes():
                    if len(body)+len(chunk)>262144:
                        raise ValueError("response_size")
                    body.extend(chunk)
                return response.status_code, bytes(body), response.headers
        finally:
            self.slots.release()


    @property
    def binding(self):
        return digest([self.model, sorted(self.approved), ADAPTER_VERSION])

    def evaluate(self, request, before=lambda _: None, finish=lambda _: None):
        request = {**request, "model": self.model}
        result = {"status": "unavailable", "attempts": []}
        encoded = json.dumps(request, allow_nan=False).encode()
        if len(encoded) > 64000 or not 0 < len(request["questions"]) <= 128:
            return {**result, "status": "unresolved", "error_class": "invalid_request"}
        if not self.key:
            return {**result, "error_class": "missing_key"}
        deadline = time.monotonic() + self.deadline
        for index in range(2):
            attempt = {"id": str(uuid.uuid4()), "started_at": time.time(), "cost": None, "http_status": 0}
            try:
                before(attempt["id"])
            except Exception:
                return {**result, "error_class": "budget_or_backpressure"}
            retry, delay = False, 0.1
            try:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise httpx.TimeoutException("deadline")
                if not self.slots.acquire(blocking=False):
                    raise httpx.TimeoutException("backpressure")
                future=self.pool.submit(self._request, encoded, remaining)
                try:
                    status_code, body, headers=future.result(timeout=remaining)
                except FutureTimeout:
                    # The bounded transport may complete later. Its result is never
                    # applied, persisted as selected, or retried past this deadline.
                    raise httpx.TimeoutException("deadline") from None
                attempt["http_status"] = status_code
                attempt["_raw_response"]=body.decode(errors="replace").replace(self.key,"[redacted]")
                if status_code == 429 or status_code >= 500:
                    attempt["error_class"], retry = "transient_http", True
                    try:
                        delay = max(0, float(headers.get("retry-after", "0.1")))
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = max(0, parsedate_to_datetime(headers["retry-after"]).timestamp()-time.time())
                        except (ValueError, KeyError, TypeError):
                            delay = 0.1
                elif status_code != 200:
                    attempt["error_class"] = "http_rejected"
                else:
                    value = json.loads(body)
                    validate(request, value)
                    result.update(status="evaluated", response=value)
                    cost = value.get("usage", {}).get("cost")
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost>=0:
                        attempt["cost"] = cost
                    if value["model"] not in self.approved:
                        result["status"], attempt["error_class"] = "unresolved", "unapproved_model"
            except (ValueError, TypeError, KeyError):
                result["status"], attempt["error_class"] = "unresolved", "schema_failure"
            except httpx.HTTPError:
                attempt["error_class"], retry = "transport", True
            attempt["finished_at"] = time.time()
            result["error_class"] = attempt.get("error_class", "")
            finish(attempt)
            attempt.pop("_raw_response",None)
            result["attempts"].append(attempt)
            if not retry or index == 1 or time.monotonic()+delay+0.1>=deadline:
                return result
            time.sleep(delay)
        return result


class Store:
    """Committed attempt reservations plus immutable first-success selection.

    SQLite BEGIN IMMEDIATE serializes short budget/lease operations across workers.
    A 10-second lease outlives the 3-second provider deadline. Crash recovery can
    repeat inference after lease expiry; it cannot overwrite a selected judgment.
    """
    def __init__(self, path, client, environment="development", tenant_cap=1, environment_cap=10):
        self.path, self.client = str(path), client
        self.environment, self.tenant_cap, self.environment_cap = environment, tenant_cap, environment_cap
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS selected(merchant TEXT, request_key TEXT, judgment TEXT, PRIMARY KEY(merchant,request_key));
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,merchant TEXT,request_key TEXT,started REAL,reserved REAL);
                CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,merchant TEXT,result TEXT);
                CREATE TABLE IF NOT EXISTS budgets(key TEXT,day TEXT,spent REAL,PRIMARY KEY(key,day));
                CREATE TABLE IF NOT EXISTS raw_outputs(id TEXT PRIMARY KEY,merchant TEXT,body TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS judgments(id TEXT PRIMARY KEY,merchant TEXT,request_key TEXT,record TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS leases(merchant TEXT PRIMARY KEY,owner TEXT,expires REAL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0.1, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    @observe_judgment
    def judge(self, identity, request):
        required = ("merchant_id", "workflow", "revision", "snapshot_hash", "rubric_hash", "policy_version", "candidate_hash", "as_of")
        if any(not identity.get(k) for k in required):
            raise ValueError("invalid_judgment_context")
        deployment, controls = control(identity["workflow"], identity["merchant_id"])
        if controls:
            if controls["model"] != self.client.model or sorted(controls["approved_models"]) != sorted(self.client.approved):
                raise ValueError("prelaunch_model_mismatch")
            identity = {**identity, "policy_version": identity["policy_version"] + ":" + deployment}
        self.purge()
        request = {**request, "model": self.client.model}
        rh = digest(request)
        key = digest([identity[k] for k in required if k != "as_of"]+[rh, self.client.binding])
        merchant, owner = identity["merchant_id"], str(uuid.uuid4())
        with self.connection() as db:
            saved = db.execute("SELECT judgment FROM selected WHERE merchant=? AND request_key=?", (merchant,key)).fetchone()
            if saved:
                return {**json.loads(saved[0]), "cache_hit": True}
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expires<?", (time.time(),))
            try:
                db.execute("INSERT INTO leases VALUES(?,?,?)", (merchant,owner,time.time()+10))
                db.execute("COMMIT")
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                raise RuntimeError("tenant_backpressure") from None
        def before(attempt):
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                day = time.strftime("%Y-%m-%d",time.gmtime())
                for budget,cap in [(self.environment+":all",(controls["daily_environment_usd"] if controls else self.environment_cap)),(self.environment+":tenant:"+merchant,(controls["daily_merchant_usd"] if controls else self.tenant_cap)),(self.environment+":draft:"+merchant+":"+identity["workflow"]+":"+identity.get("episode_id", ""),.005)]:
                    row=db.execute("SELECT spent FROM budgets WHERE key=? AND day=?",(budget,day)).fetchone()
                    used=(row[0] if row else 0)+RESERVATION
                    if used>cap:
                        raise RuntimeError("budget_exhausted")
                    db.execute("INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(key,day) DO UPDATE SET spent=excluded.spent",(budget,day,used))
                db.execute("INSERT INTO attempts VALUES(?,?,?,?,?)",(attempt,merchant,key,time.time(),RESERVATION))
                db.execute("COMMIT")
        def finish(attempt):
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if attempt.get("_raw_response"):
                    db.execute("INSERT INTO raw_outputs VALUES(?,?,?,?)",(attempt["id"],merchant,attempt["_raw_response"],time.time()))
                metadata={k:v for k,v in attempt.items() if k!="_raw_response"}
                db.execute("INSERT INTO results VALUES(?,?,?)",(attempt["id"],merchant,json.dumps(metadata)))
                if attempt["cost"] is not None and attempt["cost"]>RESERVATION:
                    for key in [self.environment+":all",self.environment+":tenant:"+merchant,self.environment+":draft:"+merchant+":"+identity["workflow"]+":"+identity.get("episode_id","")]:
                        db.execute("UPDATE budgets SET spent=spent+? WHERE key=? AND day=?",(attempt["cost"]-RESERVATION,key,time.strftime("%Y-%m-%d",time.gmtime())))
                db.execute("COMMIT")
        try:
            result=self.client.evaluate(request,before,finish)
            try:
                current, _ = control(identity["workflow"], merchant)
                if current != deployment:
                    raise ValueError("deployment_changed")
            except ValueError:
                result.update(status="unavailable", error_class="deployment_changed")
            if any(a["cost"] is not None and a["cost"]>RESERVATION for a in result["attempts"]):
                result.update(status="unresolved",error_class="cost_reservation_exceeded")
            judgment={"schema_version":"intelligence_v1","judgment_id":key,"identity":identity,"request_hash":rh,"model_binding":self.client.binding,"adapter_version":ADAPTER_VERSION,"provider":"openrouter","requested_model":self.client.model,"result":result,"cache_hit":False}
            with self.connection() as db:
                db.execute("INSERT INTO judgments VALUES(?,?,?,?,?)",(str(uuid.uuid4()),merchant,key,json.dumps(judgment),time.time()))
            if result["status"]=="evaluated":
                with self.connection() as db:
                    db.execute("INSERT OR IGNORE INTO selected VALUES(?,?,?)",(merchant,key,json.dumps(judgment)))
                    judgment=json.loads(db.execute("SELECT judgment FROM selected WHERE merchant=? AND request_key=?",(merchant,key)).fetchone()[0])
            return judgment
        finally:
            with self.connection() as db:
                db.execute("DELETE FROM leases WHERE merchant=? AND owner=?",(merchant,owner))

    def delete_tenant(self, merchant):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("raw_outputs","selected","attempts","results","leases","judgments"):
                db.execute(f"DELETE FROM {table} WHERE merchant=?", (merchant,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_plans'").fetchone():
                db.execute("DELETE FROM recovery_plans WHERE merchant=?",(merchant,))
            db.execute("DELETE FROM budgets WHERE key = ?",(self.environment+":tenant:"+merchant,))
            prefix=self.environment+":draft:"+merchant+":"
            db.execute("DELETE FROM budgets WHERE substr(key,1,?) = ?",(len(prefix),prefix))
            db.execute("COMMIT")

    def purge(self):
        """Apply journal retention to every merchant, including inactive ones."""
        from .maintenance import purge_journal
        purge_journal(self.path)


def configured_store(path):
    """Native worker construction; workflows opt in independently when implemented."""
    if os.getenv("TYPESAFE_PROVIDER", "openrouter") != "openrouter":
        raise ValueError("invalid_provider")
    client=Client(base_url=os.getenv("TYPESAFE_BASE_URL", ENDPOINT))
    tenant=float(os.getenv("TYPESAFE_DAILY_MERCHANT_USD", "1"))
    environment=float(os.getenv("TYPESAFE_DAILY_ENVIRONMENT_USD", "10"))
    if not math.isfinite(tenant) or not math.isfinite(environment) or min(tenant,environment)<=0:
        client.close()
        raise ValueError("invalid_daily_budget")
    return Store(path,client,os.getenv("TYPESAFE_ENVIRONMENT","development"),tenant,environment)
