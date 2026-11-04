"""Versioned contract reader. Validation never grants outreach authority."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import jsonschema
import rfc8785

ROOT = Path(__file__).resolve().parents[2] / 'contracts/integrations/v1'
BUNDLE = json.loads((ROOT / 'bundle.json').read_text())

def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result

def context_hash(value, field):
    return hashlib.sha256((value['version'] + '\n').encode() + rfc8785.dumps({k:v for k,v in value.items() if k != field})).hexdigest()

def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def read_contract(raw, *, merchant='', store='', subject='', episode='', expected_context_hash='', at=None):
    if len(raw.encode() if isinstance(raw, str) else raw) > 200000:
        raise ValueError('context_bounds')
    value = json.loads(raw, object_pairs_hook=_pairs)
    rfc8785.dumps(value)  # rejects invalid Unicode and unsafe numeric values
    if not isinstance(value, dict):
        raise ValueError('invalid_contract_object')
    version = value.get('version')
    if not isinstance(version, str) or version not in BUNDLE['$defs'] or '_v' not in version:
        raise ValueError('unknown_contract_version')
    schema = dict(BUNDLE, **{'$ref': '#/$defs/' + version})
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    for key, expected in dict(merchant_id=merchant, store_id=store, subject_id=subject, episode_id=episode).items():
        if key in value and expected and value[key] != expected:
            raise ValueError('identity_mismatch')
    if version == 'recovery_context_v2':
        c = value['customer_context']
        if c['merchant_id'] != value['merchant_id'] or c['as_of'] != value['as_of']:
            raise ValueError('context_binding')
        read_contract(json.dumps(c), merchant=merchant, store=store, subject=subject, episode=value['episode_id'], at=at)
        if context_hash(value, 'context_hash') != value['context_hash']:
            raise ValueError('context_hash_mismatch')
    if version == 'customer_context_v1':
        if context_hash(value, 'snapshot_hash') != value['snapshot_hash']:
            raise ValueError('snapshot_hash_mismatch')
        start, end = _time(value['as_of']), _time(value['expires_at'])
        if not 0 < (end-start).total_seconds() <= 86400 or (at and not start <= at < end):
            raise ValueError('context_horizon')
        generations, coverage_keys = {}, set()
        for c in value['coverage']:
            key = (c['connection_id'], c['capability'])
            if key in coverage_keys or (c['connection_id'] in generations and generations[c['connection_id']] != c['generation']):
                raise ValueError('coverage_binding')
            coverage_keys.add(key)
            generations[c['connection_id']] = c['generation']
            if c['verified_at'] is None:
                if c['state'] == 'complete':
                    raise ValueError('coverage_unverified')
            elif _time(c['verified_at']) > start:
                raise ValueError('future_coverage')
        seen = set()
        for r in value['evidence']:
            if any(r[k] != value[k] for k in ('merchant_id','store_id','subject_id','privacy_generation')) or _time(r['observed_at']) > start:
                raise ValueError('evidence_binding')
            if generations.get(r['connection_id']) != r['connection_generation']:
                raise ValueError('source_generation')
            if r['scope'] == 'customer' and r['scope_id'] != value['subject_id']:
                raise ValueError('customer_scope')
            if r['scope'] == 'episode' and (not episode or r['scope_id'] != episode):
                raise ValueError('episode_scope')
            key = (r['connection_id'],r['object_type'],r['object_id'],r['revision'])
            if key in seen:
                raise ValueError('duplicate_evidence')
            seen.add(key)
    if version in ('buyer_intelligence_v2','recovery_plan_v2') and (not expected_context_hash or value['context_hash'] != expected_context_hash):
        raise ValueError('plan_context_binding')
    if version.startswith('recovery_context_'):
        start = _time(value['as_of'])
        if at and start > at:
            raise ValueError('future_context')
        for f in value['feedback']:
            if f['episode_id'] != value['episode_id'] or any(_time(f[k]) > start for k in ('occurred_at','recorded_at')):
                raise ValueError('feedback_scope')
    return value
