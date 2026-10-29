"""Exercise serialized domain admission against independent trusted bindings."""

import copy
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from cart_recovery.domain.knowledge import KnowledgeError, SCHEMA, read_knowledge
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "contracts/knowledge/v1/fixtures.json").read_text())


def case(name):
    f = copy.deepcopy(next(f for f in FIXTURES if f["name"] == name))
    # Serialization disconnects candidate values from trusted receipt objects.
    payload = json.loads(json.dumps(f["payload"]))
    binding = json.loads(json.dumps(f["bindings"]))
    binding["at"] = datetime.fromisoformat(binding["at"].replace("Z", "+00:00"))
    return payload, binding


def admit(payload, binding):
    return read_knowledge(json.dumps(payload), **binding)


@pytest.mark.parametrize("name", [f["name"] for f in FIXTURES])
def test_serialized_scenarios(name):
    payload, binding = case(name)
    before = copy.deepcopy(binding)
    assert admit(payload, binding) == payload
    assert binding == before


def test_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("field,value", [
    ("merchant_id", "merchant-b"), ("store_id", "store-b"),
    ("subject_id", "shopper-b"), ("identity_revision", 2),
    ("privacy_generation", 2), ("subject_kind", "merchant"),
])
def test_candidate_cannot_change_authenticated_scope(field, value):
    payload, binding = case("explicit_preference")
    payload["scope"][field] = value
    with pytest.raises(KnowledgeError, match="scope_binding"):
        admit(payload, binding)


@pytest.mark.parametrize("field,value", [
    ("merchant_id", "merchant-b"), ("store_id", "store-b"),
    ("subject_id", "shopper-b"), ("identity_revision", 2), ("privacy_generation", 2),
])
def test_source_receipt_must_share_exact_scope(field, value):
    payload, binding = case("explicit_preference")
    binding["sources"][0]["scope"][field] = value
    with pytest.raises(KnowledgeError, match="source_binding"):
        admit(payload, binding)


@pytest.mark.parametrize("field,value", [
    ("send_authorized", True), ("consent", "granted"), ("eligible", True),
    ("confidence", 0.99), ("authority", "merchant_approved"),
])
def test_preferences_cannot_smuggle_authority(field, value):
    payload, binding = case("channel_is_not_consent")
    payload["objects"][0]["value"][field] = value
    with pytest.raises(KnowledgeError, match="invalid_schema"):
        admit(payload, binding)


@pytest.mark.parametrize("mutation,error", [
    ("missing_source", "source_binding"), ("source_revision", "source_binding"),
    ("wrong_authority", "source_claim"), ("invented_value", "source_claim"),
    ("missing_entity", "entity_binding"), ("future_source", "source_binding"),
    ("duplicate_source", "source_binding"), ("invalid_hash", "invalid_schema"),
    ("source_after_record", "source_time"), ("future_record", "record_time"),
    ("unaware_clock", "clock_binding"), ("duplicate_object", "invalid_schema"),
    ("unknown_version", "invalid_schema"), ("local_timestamp", "invalid_schema"),
])
def test_source_identity_and_record_integrity(mutation, error):
    payload, binding = case("explicit_preference")
    record = payload["objects"][0]
    if mutation == "missing_source": binding["sources"] = []
    elif mutation == "source_revision": record["sources"][0]["revision"] = 2
    elif mutation == "wrong_authority": binding["sources"][0]["authority"] = "engine_observation"
    elif mutation == "invented_value": record["value"]["value"] = "XXL"
    elif mutation == "missing_entity": binding["entities"] = []
    elif mutation == "future_source": binding["sources"][0]["observed_at"] = "2027-01-01T00:00:00Z"
    elif mutation == "duplicate_source": binding["sources"].append(copy.deepcopy(binding["sources"][0]))
    elif mutation == "invalid_hash": binding["sources"][0]["content_sha256"] = "unverified"
    elif mutation == "source_after_record": binding["sources"][0]["observed_at"] = "2026-10-29T11:00:00Z"
    elif mutation == "future_record": record["recorded_at"] = "2027-01-01T00:00:00Z"
    elif mutation == "unaware_clock": binding["at"] = datetime(2026, 10, 29)
    elif mutation == "duplicate_object": payload["objects"].append(copy.deepcopy(record))
    elif mutation == "unknown_version": payload["version"] = "knowledge_batch_v99"
    elif mutation == "local_timestamp": record["observed_at"] = "2026-10-29T10:00:00"
    with pytest.raises(KnowledgeError, match=error):
        admit(payload, binding)


@pytest.mark.parametrize("mutation,error", [
    ("no_expiry", "invalid_schema"), ("no_support_link", "derivation_input"),
    ("dangling_input", "derivation_input"), ("revoked_input", "derivation_input"),
    ("wrong_source_union", "source_binding"), ("no_model_version", "derivation_version"),
    ("invented_model_for_rule", "derivation_version"), ("expires_before_start", "validity_interval"),
    ("outlives_input", "derivation_validity"), ("promote_to_explicit", "invalid_schema"),
    ("input_recorded_later", "derivation_input"),
])
def test_inference_is_a_bounded_hypothesis(mutation, error):
    payload, binding = case("inferred_preference")
    observation, inferred = payload["objects"]
    if mutation == "no_expiry": inferred["expires_at"] = None
    elif mutation == "no_support_link": payload["links"] = []
    elif mutation == "dangling_input": inferred["derivation"]["input_ids"].append("missing")
    elif mutation == "revoked_input": observation["status"] = "revoked"
    elif mutation == "wrong_source_union": inferred["sources"][0]["source_id"] = "missing"
    elif mutation == "no_model_version": inferred["derivation"]["method"] = "model"
    elif mutation == "invented_model_for_rule": inferred["derivation"]["model_version"] = "made-up"
    elif mutation == "expires_before_start": inferred["expires_at"] = "2026-10-28T00:00:00Z"
    elif mutation == "outlives_input": observation["valid_to"] = "2026-10-30T00:00:00Z"
    elif mutation == "promote_to_explicit": inferred["kind"] = "explicit_preference"
    elif mutation == "input_recorded_later": observation["recorded_at"] = "2026-10-29T11:00:00Z"
    with pytest.raises(KnowledgeError, match=error):
        admit(payload, binding)


def test_conflicts_are_preserved_without_a_winner():
    payload, binding = case("conflicting_preferences")
    result = admit(payload, binding)
    assert [r["value"]["value"] for r in result["objects"]] == ["M", "L"]
    payload["links"] = []
    with pytest.raises(KnowledgeError, match="unmarked_conflict"):
        admit(payload, binding)


@pytest.mark.parametrize("mutation,error", [
    ("missing_link", "missing_successor"), ("dangling", "link_binding"),
    ("self_link", "link_binding"), ("wrong_assertion", "supersession_binding"),
    ("skipped_revision", "supersession_binding"), ("still_current", "supersession_binding"),
])
def test_correction_lineage(mutation, error):
    payload, binding = case("corrected_preference")
    if mutation == "missing_link": payload["links"] = []
    elif mutation == "dangling": payload["links"][0]["to"] = "missing"
    elif mutation == "self_link": payload["links"][0]["to"] = "pref-b"
    elif mutation == "wrong_assertion": payload["objects"][1]["assertion_id"] = "different"
    elif mutation == "skipped_revision": payload["objects"][1]["revision"] = 3
    elif mutation == "still_current": payload["objects"][0]["status"] = "current"
    with pytest.raises(KnowledgeError, match=error):
        admit(payload, binding)


def test_graph_edges_cannot_link_shoppers_by_similarity():
    payload, binding = case("shopper_relationship")
    payload["objects"][0]["value"]["predicate"] = "same_person"
    with pytest.raises(KnowledgeError, match="invalid_schema"):
        admit(payload, binding)


def test_even_trusted_relationship_claim_needs_typed_endpoints():
    payload, binding = case("shopper_relationship")
    payload["objects"][0]["value"]["from"]["id"] = "shopper-b"
    binding["sources"][0]["claims"][0]["value"] = copy.deepcopy(payload["objects"][0]["value"])
    with pytest.raises(KnowledgeError, match="relationship_endpoints"):
        admit(payload, binding)


def test_outcome_never_claims_conversion_lift():
    payload, binding = case("observed_outcome")
    payload["objects"][0]["value"]["attribution"] = "caused_by_message"
    with pytest.raises(KnowledgeError, match="invalid_schema"):
        admit(payload, binding)


def test_unknown_has_no_invented_evidence():
    payload, binding = case("unknown")
    payload["objects"][0]["sources"] = [{"source_id": "fake", "revision": 1}]
    with pytest.raises(KnowledgeError, match="invalid_schema"):
        admit(payload, binding)


@pytest.mark.parametrize("raw,error", [
    ('{"version": "knowledge_batch_v1", "version": "knowledge_batch_v1"}', "duplicate_key"),
    ('{} {}', "invalid_json"), ('{"x":NaN}', "invalid_schema"),
    ('"' + 'a' * 200_000 + '"', "payload_bounds"), ('{"a":', "invalid_json"),
    ('[]', "invalid_schema"), (b'\xff', "invalid_json"),
])
def test_wire_rejection(raw, error):
    _, binding = case("unknown")
    with pytest.raises(KnowledgeError, match=error):
        read_knowledge(raw, **binding)


def test_reader_runs_in_fresh_process_without_network_or_simulation():
    script = '''
import json, socket, sys
from datetime import datetime
def refuse(*args, **kwargs): raise AssertionError("network attempted")
socket.socket = refuse
from cart_recovery.domain.knowledge import read_knowledge
for f in json.load(open("contracts/knowledge/v1/fixtures.json")):
    b = f["bindings"]
    b["at"] = datetime.fromisoformat(b["at"].replace("Z", "+00:00"))
    read_knowledge(json.dumps(f["payload"]), **b)
assert not any(n.split(".")[0] in {"torch", "camel", "oasis", "zep_cloud", "redis", "flask"} for n in sys.modules)
print("12 serialized scenarios; zero network calls; no simulation, Redis or Flask imports")
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field,value", [("merchant_id", "merchant-b"), ("store_id", "store-b")])
def test_catalog_identity_cannot_cross_tenants(field, value):
    payload, binding = case("explicit_preference")
    binding["entities"][0][field] = value
    with pytest.raises(KnowledgeError, match="entity_binding"):
        admit(payload, binding)


def test_source_observation_time_cannot_be_fabricated():
    payload, binding = case("explicit_preference")
    record = payload["objects"][0]
    for field in ("observed_at", "recorded_at", "valid_from"):
        record[field] = "2026-10-29T11:00:00Z"
    with pytest.raises(KnowledgeError, match="observation_time_binding"):
        admit(payload, binding)


def test_a_correction_cannot_change_preference_dimension():
    payload, binding = case("corrected_preference")
    payload["objects"][1]["value"]["dimension"] = "color"
    binding["sources"][1]["claims"][0]["value"] = copy.deepcopy(payload["objects"][1]["value"])
    with pytest.raises(KnowledgeError, match="supersession_binding"):
        admit(payload, binding)


def test_unknown_is_distinct_from_negative_preference():
    payload, binding = case("unknown")
    result = admit(payload, binding)
    assert result["objects"][0]["value"] == {"topic": "preference", "reason": "not_observed"}
    assert result["objects"][0]["sources"] == []


def test_historical_record_admission_is_not_current_context():
    payload, binding = case("episode_constraint")
    binding["at"] = datetime.fromisoformat("2026-11-03T12:00:00+00:00")
    result = admit(payload, binding)
    assert result["objects"][0]["expires_at"] == "2026-10-30T10:00:00Z"
    assert "eligible" not in result


def test_non_utf8_json_is_rejected():
    payload, binding = case("unknown")
    with pytest.raises(KnowledgeError, match="invalid_json"):
        read_knowledge(json.dumps(payload).encode("utf-16"), **binding)


@pytest.mark.parametrize("authority", ["shopper_statement", "engine_observation"])
def test_policy_relationship_requires_merchant_approval(authority):
    payload, binding = case("approved_merchant_policy")
    binding["sources"][1]["authority"] = authority
    with pytest.raises(KnowledgeError, match="source_claim"):
        admit(payload, binding)


def test_merchant_cannot_assert_shopper_interest():
    payload, binding = case("shopper_relationship")
    binding["sources"][0]["authority"] = "merchant_approved"
    with pytest.raises(KnowledgeError, match="source_claim"):
        admit(payload, binding)
