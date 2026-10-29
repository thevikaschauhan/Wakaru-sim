"""Strict, side-effect-free admission for knowledge_batch_v1.

Trusted scope, source receipts and entity identities must come from an authorized
caller, never from the candidate payload. Admission checks a record's meaning
and provenance binding; it does not make the record current or authorize action.
"""

import json
from datetime import datetime
from pathlib import Path

import jsonschema
import rfc8785


SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "contracts/knowledge/v1/schema.json").read_text())
FORMAT = jsonschema.FormatChecker()
MAX_BYTES = 200_000


class KnowledgeError(ValueError):
    """Stable refusal code without echoing shopper data."""


def _check(value, definition=None):
    schema = SCHEMA if definition is None else {
        "$defs": SCHEMA["$defs"], "$ref": "#/$defs/" + definition,
    }
    try:
        jsonschema.Draft202012Validator(schema, format_checker=FORMAT).validate(value)
        rfc8785.dumps(value)
    except (jsonschema.ValidationError, ValueError, TypeError) as exc:
        raise KnowledgeError("invalid_schema") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeError("duplicate_key")
        result[key] = value
    return result


def _time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeError("invalid_timestamp") from exc


def _source_key(source):
    return source["source_id"], source["revision"]


def _claim_key(record):
    value = record["value"]
    if record["kind"] in ("explicit_preference", "inferred_preference"):
        return ("preference", value["dimension"], value["applies_to"]["kind"], value["applies_to"]["id"])
    if record["kind"] == "merchant_knowledge":
        return ("merchant", value["topic"], value["applies_to"]["kind"], value["applies_to"]["id"])
    if record["kind"] == "episode_state":
        return ("episode", value["episode_id"], value["constraint"])
    return None


def _overlaps(left, right):
    def end(record):
        ends = [_time(record[key]) for key in ("valid_to", "expires_at") if record[key]]
        return min(ends) if ends else datetime.max.replace(tzinfo=_time(record["valid_from"]).tzinfo)
    return max(_time(left["valid_from"]), _time(right["valid_from"])) < min(end(left), end(right))


def _validate_record(record, scope, receipts, entities, at):
    kind, value = record["kind"], record["value"]
    observed, recorded, start = (_time(record[k]) for k in ("observed_at", "recorded_at", "valid_from"))
    if not observed <= recorded <= at or start < observed:
        raise KnowledgeError("record_time")
    for key in ("valid_to", "expires_at"):
        if record[key] and _time(record[key]) <= start:
            raise KnowledgeError("validity_interval")
    if kind == "merchant_knowledge" and scope["subject_kind"] != "merchant":
        raise KnowledgeError("subject_kind")
    if kind in ("explicit_preference", "inferred_preference", "episode_state", "observation", "outcome") and scope["subject_kind"] != "shopper":
        raise KnowledgeError("subject_kind")
    refs = record["sources"]
    sources = []
    for ref in refs:
        source = receipts.get(_source_key(ref))
        if source is None or source["scope"] != scope:
            raise KnowledgeError("source_binding")
        if _time(source["observed_at"]) > observed:
            raise KnowledgeError("source_time")
        sources.append(source)
    allowed = {
        "observation": {"engine_observation"}, "outcome": {"engine_observation"},
        "merchant_knowledge": {"merchant_approved"}, "explicit_preference": {"shopper_statement"},
        "episode_state": {"engine_observation", "shopper_statement"},
        "relationship": ({"merchant_approved"} if value.get("predicate") == "governed_by"
                         else {"engine_observation", "shopper_statement"}),
    }
    if kind in allowed:
        claim = {"kind": kind, "value": value}
        if any(s["authority"] not in allowed[kind] or claim not in s["claims"] for s in sources):
            raise KnowledgeError("source_claim")
        if observed != max(_time(s["observed_at"]) for s in sources):
            raise KnowledgeError("observation_time_binding")
    referenced_entities = []
    if "applies_to" in value:
        referenced_entities.append(value["applies_to"])
    if kind == "relationship":
        left, right = value["from"], value["to"]
        if value["predicate"] == "interested_in":
            if scope["subject_kind"] != "shopper" or left != {"kind": "subject", "id": scope["subject_id"]} or right["kind"] not in ("product", "category", "brand"):
                raise KnowledgeError("relationship_endpoints")
        elif scope["subject_kind"] != "merchant" or left["kind"] != "product" or right["kind"] != "policy":
            raise KnowledgeError("relationship_endpoints")
        referenced_entities.extend([e for e in (left, right) if e["kind"] != "subject"])
    if any((e["kind"], e["id"]) not in entities for e in referenced_entities):
        raise KnowledgeError("entity_binding")


def _validate_links(records, links):
    by_id = {r["object_id"]: r for r in records}
    if len(by_id) != len(records) or len({(r["assertion_id"], r["revision"]) for r in records}) != len(records):
        raise KnowledgeError("duplicate_object")
    edges = {(e["relation"], e["from"], e["to"]) for e in links}
    successors = {}
    for relation, source, target in edges:
        if source == target or source not in by_id or target not in by_id:
            raise KnowledgeError("link_binding")
        left, right = by_id[source], by_id[target]
        if relation == "supersedes":
            if (left["assertion_id"] != right["assertion_id"] or left["revision"] != right["revision"] + 1
                    or right["status"] != "superseded" or left["kind"] != right["kind"]
                    or _claim_key(left) != _claim_key(right)
                    or _time(left["recorded_at"]) < _time(right["recorded_at"]) or target in successors):
                raise KnowledgeError("supersession_binding")
            successors[target] = source
        elif relation == "supports":
            if right["kind"] != "inferred_preference" or source not in right["derivation"]["input_ids"]:
                raise KnowledgeError("support_binding")
        elif not _claim_key(left) or _claim_key(left) != _claim_key(right) or left["value"] == right["value"]:
            raise KnowledgeError("contradiction_binding")
    for record in records:
        rid = record["object_id"]
        if record["revision"] > 1 and not any(rel == "supersedes" and src == rid for rel, src, _ in edges):
            raise KnowledgeError("missing_predecessor")
        if record["status"] == "superseded" and rid not in successors:
            raise KnowledgeError("missing_successor")
        if record["kind"] != "inferred_preference":
            continue
        derivation = record["derivation"]
        if (derivation["method"] == "model") != (derivation["model_version"] is not None):
            raise KnowledgeError("derivation_version")
        sources = set()
        for iid in derivation["input_ids"]:
            item = by_id.get(iid)
            if (item is None or item["kind"] != "observation" or item["status"] != "current"
                    or ("supports", iid, rid) not in edges or _time(item["recorded_at"]) > _time(record["recorded_at"])):
                raise KnowledgeError("derivation_input")
            # A derived assertion cannot outlive the evidence's declared validity.
            if _time(item["valid_from"]) > _time(record["valid_from"]):
                raise KnowledgeError("derivation_validity")
            if any(item[k] and _time(record["expires_at"]) > _time(item[k]) for k in ("valid_to", "expires_at")):
                raise KnowledgeError("derivation_validity")
            sources.update(_source_key(s) for s in item["sources"])
        if sources != {_source_key(s) for s in record["sources"]}:
            raise KnowledgeError("derivation_sources")
    for index, left in enumerate(records):
        for right in records[index + 1:]:
            if (left["status"] == right["status"] == "current" and _claim_key(left)
                    and _claim_key(left) == _claim_key(right) and left["value"] != right["value"] and _overlaps(left, right)):
                if not any(("contradicts", a, b) in edges for a, b in [(left["object_id"], right["object_id"]), (right["object_id"], left["object_id"])]):
                    raise KnowledgeError("unmarked_conflict")


def read_knowledge(raw, *, scope, sources, entities, at):
    """Read one bounded aggregate against caller-authenticated bindings.

    `scope` is the exact Engine-issued subject/store/generation binding. `sources`
    are verified application-owned source receipts including exact admitted claims.
    `entities` are catalog receipts bound to that same merchant and store. `at` is
    an aware server timestamp. None of these arguments may come from the payload.
    Returned historical/future records still require lifecycle/context filtering.
    """
    if not isinstance(raw, (bytes, str)):
        raise KnowledgeError("invalid_json")
    try:
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_BYTES:
            raise KnowledgeError("payload_bounds")
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise KnowledgeError("invalid_json") from exc
    _check(value)
    _check(scope, "scope")
    if value["scope"] != scope or (scope["subject_kind"] == "merchant" and scope["subject_id"] != scope["merchant_id"]):
        raise KnowledgeError("scope_binding")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise KnowledgeError("clock_binding")
    if len(sources) > 256 or len(entities) > 256:
        raise KnowledgeError("binding_bounds")
    receipts = {}
    for source in sources:
        _check(source, "source")
        if source["scope"] != scope or _source_key(source) in receipts or _time(source["observed_at"]) > at:
            raise KnowledgeError("source_binding")
        receipts[_source_key(source)] = source
    entity_keys = set()
    for receipt in entities:
        _check(receipt, "catalog_entity")
        if any(receipt[k] != scope[k] for k in ("merchant_id", "store_id")):
            raise KnowledgeError("entity_binding")
        entity = receipt["entity"]
        entity_keys.add((entity["kind"], entity["id"]))
    for record in value["objects"]:
        _validate_record(record, scope, receipts, entity_keys, at)
    _validate_links(value["objects"], value["links"])
    return value
