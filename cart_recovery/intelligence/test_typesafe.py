import copy
import json
from pathlib import Path
import sqlite3
import threading
import time

import httpx
import pytest
from .typesafe import Client, Store, validate

CONTRACTS=Path(__file__).resolve().parents[2]/'contracts/intelligence/v1'

@pytest.mark.parametrize('case',json.loads((CONTRACTS/'conformance.json').read_text()),ids=lambda c:c['name'])
def test_shared_corpus(case):
    if case['valid']:
        validate(case['request'],case['response'])
    else:
        with pytest.raises((ValueError, TypeError, KeyError)):
            validate(case['request'],case['response'])

def test_storage_replay_isolation_budget_and_outage(tmp_path):
    fixture=json.loads((CONTRACTS/'conformance.json').read_text())[0]
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(200,json=fixture['response'])
    client=Client(key='fixture',approved_models=[fixture['response']['model']],transport=httpx.MockTransport(transport))
    store=Store(tmp_path/'judgments.db',client)
    identity=dict(merchant_id='m1',workflow='draft_claims',revision='1',snapshot_hash='hash',rubric_hash='rubric',policy_version='v1',candidate_hash='candidates',as_of='2026-09-22',trace_id='trace',episode_id='episode')
    first=store.judge(identity,fixture['request'])
    replay=Store(tmp_path/'judgments.db',client).judge(identity,fixture['request'])
    assert first['judgment_id']==replay['judgment_id'] and replay['cache_hit'] and len(calls)==1
    assert not store.judge({**identity,'merchant_id':'m2'},fixture['request'])['cache_hit']
    assert not store.judge({**identity,'revision':'2'},fixture['request'])['cache_hit']
    capped=Store(tmp_path/'capped.db',client,tenant_cap=.0001)
    assert capped.judge(identity,fixture['request'])['result']['status']=='unavailable'
    assert len(calls)==3
    store.delete_tenant('m1')
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM selected WHERE merchant='m1'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM selected WHERE merchant='m2'").fetchone()[0]==1
    client.close()

def test_retry_failure_and_revision():
    fixture=json.loads((CONTRACTS/'conformance.json').read_text())[0]
    for code,want in [(429,2),(503,2),(401,1),(302,1)]:
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(code,json={},headers={'Retry-After':'0'})
        client=Client(key='fixture',transport=httpx.MockTransport(transport))
        assert client.evaluate(fixture['request'])['status']=='unavailable'
        assert len(calls)==want
        client.close()
    client=Client(key='fixture',approved_models=['different'],transport=httpx.MockTransport(lambda r:httpx.Response(200,json=fixture['response'])))
    assert client.evaluate(fixture['request'])['status']=='unresolved'
    client.close()

def test_crash_lease_and_concurrency(tmp_path):
    fixture=json.loads((CONTRACTS/'conformance.json').read_text())[0]
    calls=[]
    def transport(request):
        calls.append(1);time.sleep(.1)
        return httpx.Response(200,json=fixture['response'])
    client=Client(key='fixture',approved_models=[fixture['response']['model']],transport=httpx.MockTransport(transport))
    store=Store(tmp_path/'db',client)
    identity=dict(merchant_id='m',workflow='draft_claims',revision='1',snapshot_hash='h',rubric_hash='r',policy_version='v',candidate_hash='c',as_of='2026-09-22',trace_id='t',episode_id='d')
    results=[]
    def run():
        try: results.append(store.judge(identity,fixture['request']))
        except RuntimeError: pass
    workers=[threading.Thread(target=run) for _ in range(5)]
    for w in workers:w.start()
    for w in workers:w.join()
    assert len(calls)==1 and len({r['judgment_id'] for r in results})==1
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO leases VALUES(?,?,?)',('m','dead-worker',time.time()-1))
    assert store.judge({**identity,'revision':'2'},fixture['request'])['result']['status']=='evaluated'
    client.close()

def test_old_and_new_contracts():
    import jsonschema
    payloads=json.loads((CONTRACTS/'payload-fixtures.json').read_text())
    assert 'semantic' not in payloads['legacy']['checks'][0]
    jsonschema.validate(payloads['semantic_review']['checks'][1]['semantic'],json.loads((CONTRACTS/'semantic-validation.schema.json').read_text()))

def test_total_deadline_discards_late_response():
    fixture=json.loads((CONTRACTS/'conformance.json').read_text())[0]
    def slow(request):
        time.sleep(.15)
        return httpx.Response(200,json=fixture['response'])
    client=Client(key='fixture',deadline=.02,approved_models=[fixture['response']['model']],transport=httpx.MockTransport(slow))
    started=time.monotonic()
    result=client.evaluate(fixture['request'])
    assert time.monotonic()-started<.1
    assert result['status']=='unavailable' and len(result['attempts'])==1
    time.sleep(.2)
    assert result['status']=='unavailable' and 'response' not in result
    client.close()
