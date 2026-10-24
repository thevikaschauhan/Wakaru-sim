"""C8 bounded interpretation. No legacy journals, LLM, Zep or send authority.

Statements are untrusted evidence for selecting approved assistance, never facts
about the current abandonment. An injected selector may rank candidates but
cannot introduce claims, change scope or override the review-only outcome.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

import rfc8785

from ..integration_privacy import PrivacyStop
from .integration_contracts import read_contract, context_hash, _pairs


def digest(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_input(raw, *, merchant, store, subject, episode, now):
    if not all((merchant, store, subject, episode)) or len(raw) > 200000:
        raise ValueError("bounded_context_invalid")
    value = json.loads(raw, object_pairs_hook=_pairs)
    if set(value) != {"version", "context", "buyer_snapshot_hash", "product_ids", "statements", "source_keys"} or value["version"] != "bounded_context_c8_v1":
        raise ValueError("bounded_context_invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", value["buyer_snapshot_hash"]):
        raise ValueError("bounded_context_invalid")
    sources = value["source_keys"]
    products = value["product_ids"]
    if not isinstance(sources, list) or not 0 < len(sources) <= 100 or len(set(sources)) != len(sources) or not all(re.fullmatch(r"[a-f0-9]{64}", k) for k in sources):
        raise ValueError("bounded_context_invalid")
    if not isinstance(products, list) or len(products) > 100 or len(set(products)) != len(products) or not all(isinstance(p, str) and 0 < len(p) <= 256 for p in products):
        raise ValueError("bounded_context_invalid")
    context = read_contract(json.dumps(value["context"]), merchant=merchant, store=store, subject=subject, episode=episode, at=now)
    if context["version"] != "recovery_context_v2":
        raise ValueError("bounded_context_invalid")
    customer = context["customer_context"]
    facts = {f["id"]: f for f in context["approved_facts"]}
    if len(facts) != len(context["approved_facts"]):
        raise ValueError("bounded_context_invalid")
    refs = {digest(r) for r in customer["evidence"]}
    if not isinstance(value["statements"], list) or len(value["statements"]) > 30:
        raise ValueError("bounded_context_invalid")
    eligible, seen = {}, set()
    for statement in value["statements"]:
        if set(statement) != {"reference", "text", "author", "visibility", "expires_at"}:
            raise ValueError("bounded_context_invalid")
        ref = statement["reference"]
        h = digest(ref)
        text = statement["text"]
        if h not in refs or h in seen or statement["author"] != "customer" or statement["visibility"] != "public" or not isinstance(text, str) or not 0 < len(text.encode()) <= 2000 or ref["permitted_use"] != "copy_eligible" or ref["object_type"] != "customer_statement" or moment(statement["expires_at"]) < moment(customer["expires_at"]):
            raise ValueError("bounded_context_invalid")
        if hashlib.sha256(text.encode()).hexdigest() != ref["span_hash"]:
            raise ValueError("bounded_context_invalid")
        seen.add(h)
        if ref["source_updated_at"] is None or moment(ref["source_updated_at"]) > moment(context["as_of"]):
            raise ValueError("bounded_context_invalid")
        if not any(c["connection_id"] == ref["connection_id"] and c["state"] == "complete" and c["scope"] == subject for c in customer["coverage"]):
            continue
        if ref["scope"] == "order" or ref["scope"] == "product" and ref["scope_id"] not in products:
            continue
        eligible[h] = statement
    return value, context, facts, eligible


def select_assistance(facts, statements):
    """Conservative baseline relevance, with no motive classification or prose.

    Future semantic selection may replace this function behind the same checks.
    Unknown topics stay empty. Inventory and delivery promises are not selected.
    """
    selected = []
    for source, statement in sorted(statements.items()):
        words = set(re.findall(r"[a-z]+", statement["text"].lower()))
        for fact_id, fact in sorted(facts.items()):
            if fact["kind"] == "shipping_threshold" and words & {"shipping", "postage"}:
                selected.append((fact_id, source))
            elif fact["kind"] == "value_prop" and words & {"product", "material", "size", "sizing", "fit"}:
                selected.append((fact_id, source))
    return selected


def interpret(raw, *, merchant, store, subject, episode, now, selector=select_assistance):
    value, context, facts, statements = read_input(raw, merchant=merchant, store=store, subject=subject, episode=episode, now=now)
    customer = context["customer_context"]
    clear = customer["identity_status"] == "resolved" and not set(customer["constraints"]) - {"support_unchecked", "live_send_gates_required"}
    choices = selector(json.loads(json.dumps(facts)), json.loads(json.dumps(statements))) if clear else []
    if not isinstance(choices, list) or len(choices) > 900:
        raise ValueError("bounded_selection_invalid")
    ids, sources = set(), set()
    for fact_id, source in choices:
        fact = facts.get(fact_id)
        if not fact or source not in statements or fact["status"] != "active" or fact["scope_resolved"] is not True:
            raise ValueError("bounded_selection_invalid")
        if len(ids) >= 3 and fact_id not in ids:
            continue
        ids.add(fact_id)
        sources.add(source)
    plan = dict(version="recovery_plan_v2", plan_id=digest(value), revision="", based_on_snapshot_sha256=value["buyer_snapshot_hash"], episode_id=episode,
                status="needs_review", action="review", mode="assist", fact_ids=sorted(ids), fact_revisions={i: facts[i]["revision"] for i in sorted(ids)},
                allowed_claims=[facts[i]["text"] for i in sorted(ids)], judgment_ids=[], eligible_actions=context["eligible_actions"], reason_codes=["bounded_context_review_only"],
                expires_at=customer["expires_at"], allow_incentive=False, tone="neutral", resume_trigger="merchant_review", scores={}, context_hash=context["context_hash"],
                source_refs=[statements[s]["reference"] for s in sorted(sources)])
    plan["revision"] = context_hash(plan, "revision")
    read_contract(json.dumps(plan), merchant=merchant, store=store, subject=subject, episode=episode, expected_context_hash=context["context_hash"], at=now)
    return plan


class BoundedService:
    """C5-owned sync/queued analysis. Queue entries contain only an opaque ID.

    No worker is started here. Each explicit run rechecks privacy before and
    after interpretation, and polling never serves a Redis-cached result.
    """
    def __init__(self, privacy_store, selector=select_assistance, clock=lambda: datetime.now(timezone.utc)):
        self.store = privacy_store
        self.selector = selector
        self.clock = clock

    def submit(self, raw, *, merchant, store, subject, episode, now):
        value, context, _, _ = read_input(raw, merchant=merchant, store=store, subject=subject, episode=episode, now=now)
        customer = context["customer_context"]
        job = digest(value)
        self.store.put(merchant, store, artifact_id=job, subject=hashlib.sha256(subject.encode()).hexdigest(), generation=customer["privacy_generation"],
                       kind="pending_job", sources=sorted(value["source_keys"]), payload=value, expires=moment(customer["expires_at"]).timestamp(), now=now.timestamp())
        return job

    def run(self, merchant, store, job, *, now):
        value = self.store.read(merchant, store, job, now=now.timestamp())
        context = value["context"]
        customer = context["customer_context"]
        try:
            return self.result(merchant, store, job, now=now)
        except PrivacyStop:
            pass  # Missing result may be computed; the write fence still applies.
        plan = interpret(json.dumps(value), merchant=merchant, store=store, subject=customer["subject_id"], episode=context["episode_id"], now=now, selector=self.selector)
        now = self.clock()
        self.store.put(merchant, store, artifact_id=job+":plan", subject=hashlib.sha256(customer["subject_id"].encode()).hexdigest(), generation=customer["privacy_generation"],
                       kind="plan", sources=sorted(value["source_keys"]), payload=plan, expires=moment(customer["expires_at"]).timestamp(), now=now.timestamp())
        return self.result(merchant, store, job, now=now)

    def result(self, merchant, store, job, *, now):
        return self.store.read(merchant, store, job+":plan", now=now.timestamp())

    def analyze(self, raw, **binding):
        job = self.submit(raw, **binding)
        return self.run(binding["merchant"], binding["store"], job, now=binding["now"])
