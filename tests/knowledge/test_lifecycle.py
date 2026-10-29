import copy
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import psycopg
import pytest

from cart_recovery.domain.knowledge import KnowledgeError
from cart_recovery.domain.store import KnowledgeStore, StoreConflict, digest
from conftest import PG, ROOT

KEY = b'fixture-only-fence-key-at-least-32-bytes'


def fixture(name='explicit_preference', merchant='merchant-a'):
    f = copy.deepcopy(next(x for x in json.loads((ROOT/'contracts/knowledge/v1/fixtures.json').read_text()) if x['name']==name))
    # Rebase synthetic fixture timestamps around actual server time; no frozen test clock in store.
    base = datetime(2026,10,29,10,tzinfo=timezone.utc)
    offset = datetime.now(timezone.utc)-timedelta(hours=1)-base
    def convert(v):
        if isinstance(v,dict):return {k:convert(val) for k,val in v.items()}
        if isinstance(v,list):return [convert(val) for val in v]
        if isinstance(v,str) and v.endswith('Z') and v.startswith('2026-'):
            return (datetime.fromisoformat(v.replace('Z','+00:00'))+offset).isoformat().replace('+00:00','Z')
        if v=='merchant-a':return merchant
        return v
    f=convert(f)
    for r in f['bindings']['sources']:
        r['content_sha256']=hashlib.sha256(json.dumps(r['claims']).encode()).hexdigest()
    return f


def seed(store,f):
    b=f['bindings'];store.provision(b['scope'])
    for source in b['sources']:store.capture_source(b['scope'],source,json.dumps(source['claims']).encode())
    for entity in b['entities']:store.capture_entity(b['scope'],entity)


def save(store,f,version=0,command='first'):
    return store.save(json.dumps(f['payload']),scope=f['bindings']['scope'],command_id=command,expected_version=version)


@pytest.fixture
def store(database):
    s=KnowledgeStore(database,KEY);s.bootstrap_empty();return s


def test_restart_and_idempotency_are_durable(store,database):
    f=fixture();seed(store,f);assert save(store,f)=={'version':1,'replayed':False}
    result=subprocess.run([sys.executable,'-c','''
import json,sys
from cart_recovery.domain.store import KnowledgeStore
s=KnowledgeStore(sys.argv[1],bytes.fromhex(sys.argv[2]))
f=json.loads(sys.stdin.read())
assert s.current(f['bindings']['scope'])['objects'][0]['value']['value']=='M'
assert s.save(json.dumps(f['payload']),scope=f['bindings']['scope'],command_id='first',expected_version=0)=={'version':1,'replayed':True}
''',database,KEY.hex()],input=json.dumps(f),text=True,capture_output=True,cwd=ROOT)
    assert result.returncode==0,result.stderr
    with psycopg.connect(database) as db:
        assert db.execute('SELECT count(*) FROM knowledge_data.commands').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM knowledge_retained.events').fetchone()[0]==1


def test_command_conflict_and_immutable_records(store):
    f=fixture();seed(store,f);save(store,f)
    with pytest.raises(StoreConflict,match='idempotency_conflict'):save(store,f,1)
    changed=copy.deepcopy(f);changed['payload']['objects'][0]['recorded_at']=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    with pytest.raises(StoreConflict,match='immutable_record'):save(store,changed,1,'modified')
    assert store.current(f['bindings']['scope'])['version']==1


def test_correction_history_and_stale_cas(store):
    f=fixture('corrected_preference');first=copy.deepcopy(f)
    first['payload']['objects']=first['payload']['objects'][:1];first['payload']['objects'][0]['status']='current';first['payload']['links']=[]
    seed(store,f);save(store,first)
    save(store,f,1,'correction')
    result=store.current(f['bindings']['scope']);assert len(result['objects'])==1 and result['objects'][0]['value']['value']=='L'
    exported=store.export_subject(f['bindings']['scope']);assert len(exported['payload']['objects'])==2
    with pytest.raises(StoreConflict,match='stale_version'):save(store,f,1,'stale')


def test_parallel_corrections_have_one_winner(store):
    f=fixture();seed(store,f);save(store,f)
    candidates=[]
    for i,value in enumerate(['L','S']):
        new=copy.deepcopy(f);old=new['payload']['objects'][0];old['status']='superseded'
        record=copy.deepcopy(old);record.update(object_id='next-'+str(i),revision=2,status='current');record['value']['value']=value
        record['sources']=[{'source_id':'source-next-'+str(i),'revision':1}]
        receipt=copy.deepcopy(new['bindings']['sources'][0]);receipt['source_id']=record['sources'][0]['source_id'];receipt['claims']=[{'kind':'explicit_preference','value':record['value']}]
        body=json.dumps(receipt['claims']).encode();receipt['content_sha256']=hashlib.sha256(body).hexdigest();store.capture_source(f['bindings']['scope'],receipt,body)
        new['payload']['objects'].append(record);new['payload']['links']=[{'relation':'supersedes','from':record['object_id'],'to':old['object_id']}];candidates.append(new)
    barrier=Barrier(2)
    def apply(i):
        barrier.wait()
        try:return save(store,candidates[i],1,'parallel-'+str(i))
        except StoreConflict as exc:return str(exc)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(apply,[0,1]))
    assert sum(isinstance(r,dict) for r in results)==1 and 'stale_version' in results
    assert store.current(f['bindings']['scope'])['version']==2


def test_source_content_revision_and_tenant_binding(store):
    f=fixture();seed(store,f);b=f['bindings'];receipt=copy.deepcopy(b['sources'][0])
    with pytest.raises(StoreConflict,match='source_content_binding'):store.capture_source(b['scope'],receipt,b'invented')
    receipt['claims'][0]['value']['value']='XXL';body=json.dumps(receipt['claims']).encode();receipt['content_sha256']=hashlib.sha256(body).hexdigest()
    with pytest.raises(StoreConflict,match='source_revision_conflict'):store.capture_source(b['scope'],receipt,body)
    wrong=copy.deepcopy(b['scope']);wrong['store_id']='another-store'
    with pytest.raises(StoreConflict):store.current(wrong)
    wrong=copy.deepcopy(b['scope']);wrong['merchant_id']='another-merchant'
    with pytest.raises(StoreConflict):store.current(wrong)


def test_withdrawal_removes_observation_and_derived_memory(store):
    f=fixture('inferred_preference');seed(store,f);save(store,f);scope=f['bindings']['scope']
    assert len(store.current(scope)['objects'])==2
    source=f['payload']['objects'][0]['sources'][0]
    assert store.withdraw_source(scope,source)==2
    assert store.withdraw_source(scope,source)==2
    assert store.current(scope)['objects']==[]
    with pytest.raises(KnowledgeError):save(store,f,2,'late')
    receipt=f['bindings']['sources'][0]
    with pytest.raises(StoreConflict,match='source_withdrawn'):store.capture_source(scope,receipt,json.dumps(receipt['claims']).encode())


def test_revocation_is_terminal(store):
    f=fixture();seed(store,f);save(store,f)
    revoked=copy.deepcopy(f);revoked['payload']['objects'][0]['status']='revoked';save(store,revoked,1,'revoke')
    assert store.current(f['bindings']['scope'])['objects']==[]
    with pytest.raises(StoreConflict,match='terminal_record'):save(store,f,2,'resurrect')


def test_expired_and_future_records_are_not_current(store):
    f=fixture('episode_constraint');f['payload']['objects'][0]['expires_at']=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat().replace('+00:00','Z')
    seed(store,f);save(store,f);assert store.current(f['bindings']['scope'])['objects']==[]
    assert len(store.export_subject(f['bindings']['scope'])['payload']['objects'])==1


def test_conflict_edges_survive_persistence(store):
    f=fixture('conflicting_preferences');seed(store,f);save(store,f)
    result=store.current(f['bindings']['scope']);assert len(result['objects'])==2
    assert result['links'][0]['relation']=='contradicts'


def test_identity_revision_fences_late_reads_writes_and_sources(store):
    f=fixture();seed(store,f);save(store,f);old=f['bindings']['scope'];new={**old,'identity_revision':2,'privacy_generation':2}
    store.revise_identity(old,new)
    assert store.current(new)['objects']==[]
    with pytest.raises(StoreConflict,match='subject_fenced'):save(store,f,1,'late')
    with pytest.raises(StoreConflict,match='subject_fenced'):store.current(old)
    with pytest.raises(StoreConflict,match='subject_fenced'):store.erase_subject(old)


def test_subject_erasure_removes_all_payloads_but_keeps_stop(store,database):
    f=fixture();seed(store,f);save(store,f);scope=f['bindings']['scope'];store.erase_subject(scope);store.erase_subject(scope)
    with psycopg.connect(database) as db:
        for table in ('subjects','sources','catalog','commands'):
            assert db.execute('SELECT count(*) FROM knowledge_data.'+table).fetchone()[0]==0
        assert db.execute('SELECT erased FROM knowledge_retained.subjects').fetchone()[0]
    with pytest.raises(StoreConflict):store.current(scope)
    with pytest.raises(StoreConflict):store.provision(scope)
    with pytest.raises(StoreConflict):save(store,f,1,'late')
    checkpoint=json.dumps(store.retained_checkpoint());assert 'shopper-a' not in checkpoint and 'merchant-a' not in checkpoint


def test_merchant_erasure_blocks_new_subjects_and_preserves_other_tenant(store):
    a,b=fixture(),fixture(merchant='merchant-b');seed(store,a);seed(store,b);save(store,a);save(store,b)
    store.erase_merchant('merchant-a')
    assert len(store.current(b['bindings']['scope'])['objects'])==1
    new={**a['bindings']['scope'],'subject_id':'never-seen'}
    with pytest.raises(StoreConflict):store.provision(new)


def test_quarantine_and_wrong_fence_key_fail_closed(store,database):
    f=fixture();seed(store,f);save(store,f);store.quarantine()
    with pytest.raises(StoreConflict):store.current(f['bindings']['scope'])
    with pytest.raises(StoreConflict):save(store,f,1,'late')
    wrong=KnowledgeStore(database,b'another-32-byte-fence-key-not-the-same')
    with pytest.raises(StoreConflict):wrong.current(f['bindings']['scope'])
    store.release_quarantine();assert len(store.current(f['bindings']['scope'])['objects'])==1


def test_real_data_backup_restores_under_current_erasure_fences(store,database,cluster,tmp_path):
    f=fixture();seed(store,f);save(store,f)
    backup=tmp_path/'data.sql'
    subprocess.run([str(PG/'pg_dump'),'--dbname',database,'--data-only','--schema=knowledge_data','--file',str(backup)],check=True,capture_output=True)
    store.erase_subject(f['bindings']['scope']);checkpoint=store.retained_checkpoint()
    # Load old payload rows into the same disposable DB while application admission is quarantined.
    store.quarantine()
    subprocess.run([str(PG/'psql'),'--dbname',database,'--set','ON_ERROR_STOP=1','--file',str(backup)],check=True,capture_output=True)
    with pytest.raises(StoreConflict):store.current(f['bindings']['scope'])
    store.restore_fences(checkpoint,expected_digest=digest(checkpoint));store.release_quarantine()
    with pytest.raises(StoreConflict):store.current(f['bindings']['scope'])
    with psycopg.connect(database) as db:assert db.execute('SELECT count(*) FROM knowledge_data.subjects').fetchone()[0]==0


def test_stale_checkpoint_and_backup_gap_cannot_be_released(store,database):
    f=fixture();seed(store,f);save(store,f);old=store.retained_checkpoint()
    store.withdraw_source(f['bindings']['scope'],f['payload']['objects'][0]['sources'][0]);store.quarantine()
    with pytest.raises(StoreConflict,match='checkpoint_regression'):store.restore_fences(old,expected_digest=digest(old))
    # Simulate restoring an older data snapshot while retaining current fences.
    with psycopg.connect(database) as db:db.execute('UPDATE knowledge_data.subjects SET data_version=1')
    with pytest.raises(StoreConflict,match='restore_gap'):store.release_quarantine()
    with pytest.raises(StoreConflict):store.current(f['bindings']['scope'])


def test_erasure_and_writer_race_cannot_resurrect(store,database):
    f=fixture();seed(store,f);scope=f['bindings']['scope'];barrier=Barrier(2)
    def writer():
        barrier.wait()
        try:return save(store,f)
        except StoreConflict:return 'fenced'
    def eraser():barrier.wait();store.erase_subject(scope)
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(writer);two=pool.submit(eraser);one.result();two.result()
    with pytest.raises(StoreConflict):store.current(scope)
    with psycopg.connect(database) as db:assert db.execute('SELECT count(*) FROM knowledge_data.subjects').fetchone()[0]==0


def test_erasure_before_first_write_still_fences_subject(store):
    f=fixture();scope=f['bindings']['scope'];store.erase_subject(scope)
    with pytest.raises(StoreConflict):seed(store,f)
    with pytest.raises(StoreConflict):save(store,f)


def test_partial_transaction_failure_does_not_publish_memory(store,database,monkeypatch):
    f=fixture();seed(store,f)
    def failed_event(*args):raise RuntimeError('fixture failure after data write')
    monkeypatch.setattr(store,'event',failed_event)
    with pytest.raises(RuntimeError):save(store,f)
    assert store.current(f['bindings']['scope'])=={'scope':f['bindings']['scope'],'version':0,'objects':[],'links':[]}
    with psycopg.connect(database) as db:assert db.execute('SELECT count(*) FROM knowledge_data.commands').fetchone()[0]==0


def test_future_effective_policy_is_not_current(store):
    f=fixture('approved_merchant_policy');r=f['payload']['objects'][0]
    r['valid_from']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace('+00:00','Z')
    seed(store,f);save(store,f)
    assert 'merchant_knowledge' not in [r['kind'] for r in store.current(f['bindings']['scope'])['objects']]


def test_merchant_export_has_exact_scope(store):
    a,b=fixture(),fixture(merchant='merchant-b');seed(store,a);seed(store,b);save(store,a);save(store,b)
    result=store.export_merchant('merchant-a');assert len(result)==1 and result[0]['scope']['merchant_id']=='merchant-a'
    store.erase_merchant('merchant-a')
    with pytest.raises(StoreConflict):store.export_merchant('merchant-a')


@pytest.mark.parametrize('mutation',['negative_version','duplicate_subject','wrong_tenant','wrong_digest'])
def test_checkpoint_validation_is_atomic(store,mutation):
    f=fixture();seed(store,f);save(store,f);checkpoint=store.retained_checkpoint();store.quarantine()
    altered=copy.deepcopy(checkpoint)
    if mutation=='negative_version':altered['subjects'][0][4]=-1
    elif mutation=='duplicate_subject':altered['subjects'].append(copy.deepcopy(altered['subjects'][0]));altered['subjects'][-1][4]=3
    elif mutation=='wrong_tenant':altered['subjects'][0][1]='b'*64
    expected=digest(altered) if mutation!='wrong_digest' else '0'*64
    with pytest.raises(StoreConflict):store.restore_fences(altered,expected_digest=expected)
    assert store.retained_checkpoint()==checkpoint


def test_missing_sources_are_never_supplied_by_candidate(store):
    f=fixture();store.provision(f['bindings']['scope'])
    for entity in f['bindings']['entities']:store.capture_entity(f['bindings']['scope'],entity)
    with pytest.raises(KnowledgeError,match='source_binding'):save(store,f)


def test_replayed_command_after_erase_does_not_return_success(store):
    f=fixture();seed(store,f);save(store,f);store.erase_subject(f['bindings']['scope'])
    with pytest.raises(StoreConflict):save(store,f)


def test_pending_source_withdrawal_fences_late_capture(store):
    f=fixture();scope=f['bindings']['scope'];store.provision(scope)
    store.withdraw_source(scope,f['payload']['objects'][0]['sources'][0])
    source=f['bindings']['sources'][0]
    with pytest.raises(StoreConflict,match='source_withdrawn'):store.capture_source(scope,source,json.dumps(source['claims']).encode())


def test_database_restart_preserves_memory(store,cluster):
    import os
    f=fixture();seed(store,f);save(store,f)
    _,folder=cluster
    subprocess.run([str(PG/'pg_ctl'),'-D',str(folder/'data'),'-l',str(folder/'server.log'),'-m','fast','-w','restart'],check=True,capture_output=True,timeout=30,env={**os.environ,'LC_ALL':'C','LANG':'C'})
    assert store.current(f['bindings']['scope'])['objects'][0]['value']['value']=='M'
    assert save(store,f)['replayed'] is True


def test_restore_rejects_merchant_binding_drift(store,database):
    f=fixture();seed(store,f);save(store,f);store.quarantine()
    with psycopg.connect(database) as db:
        db.execute("INSERT INTO knowledge_retained.tenants VALUES(%s,false)",('b'*64,))
        db.execute('UPDATE knowledge_retained.subjects SET tenant_key=%s',('b'*64,))
    with pytest.raises(StoreConflict,match='restore_gap'):store.release_quarantine()


def test_fresh_restore_requires_independent_current_fences(store,database,cluster,tmp_path):
    import uuid
    from psycopg import sql
    f=fixture();seed(store,f);save(store,f)
    backup=tmp_path/'before-erase.sql'
    subprocess.run([str(PG/'pg_dump'),'--dbname',database,'--data-only','--schema=knowledge_data','--file',str(backup)],check=True,capture_output=True)
    store.erase_subject(f['bindings']['scope']);checkpoint=store.retained_checkpoint()
    params,_=cluster;name='restore_'+uuid.uuid4().hex
    with psycopg.connect(**params,autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn=psycopg.conninfo.make_conninfo(**{**params,'dbname':name})
    try:
        with psycopg.connect(dsn) as db:db.execute((ROOT/'cart_recovery/domain/migrations/001_knowledge.sql').read_text())
        subprocess.run([str(PG/'psql'),'--dbname',dsn,'--set','ON_ERROR_STOP=1','--file',str(backup)],check=True,capture_output=True)
        restored=KnowledgeStore(dsn,KEY)
        with pytest.raises(StoreConflict):restored.current(f['bindings']['scope'])
        with pytest.raises(StoreConflict):restored.bootstrap_empty()
        restored.restore_fences(checkpoint,expected_digest=digest(checkpoint));restored.release_quarantine()
        with pytest.raises(StoreConflict):restored.current(f['bindings']['scope'])
        with psycopg.connect(dsn) as db:
            for table in ('subjects','sources','catalog','commands'):
                assert db.execute('SELECT count(*) FROM knowledge_data.'+table).fetchone()[0]==0
    finally:
        with psycopg.connect(**params,autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_source_withdrawal_does_not_freeze_unrelated_future_knowledge(store):
    f=fixture('inferred_preference');seed(store,f);save(store,f)
    store.withdraw_source(f['bindings']['scope'],f['payload']['objects'][0]['sources'][0])
    new=fixture();seed(store,new)
    f['payload']['objects'].extend(new['payload']['objects'])
    assert save(store,f,2,'unrelated-new-preference')['version']==3
    result=store.current(f['bindings']['scope'])
    assert [r['kind'] for r in result['objects']]==['explicit_preference']


def test_new_assertion_cannot_reuse_withdrawn_source(store):
    f=fixture();seed(store,f);save(store,f)
    store.withdraw_source(f['bindings']['scope'],f['payload']['objects'][0]['sources'][0])
    forged=copy.deepcopy(f['payload']['objects'][0]);forged.update(object_id='new-id',assertion_id='new-assertion')
    f['payload']['objects'].append(forged)
    with pytest.raises(StoreConflict,match='source_withdrawn'):save(store,f,2,'reuse')


def test_restore_rejects_missing_source_receipts(store,database):
    f=fixture();seed(store,f);save(store,f);store.quarantine()
    with psycopg.connect(database) as db:db.execute('DELETE FROM knowledge_data.sources')
    with pytest.raises(KnowledgeError):store.release_quarantine()
    with pytest.raises(StoreConflict):store.current(f['bindings']['scope'])


def test_catalog_matches_privacy_inventory(database):
    inventory=json.loads((ROOT/'contracts/knowledge/lifecycle-v1/inventory.json').read_text())
    with psycopg.connect(database) as db:
        rows=db.execute("SELECT table_schema||'.'||table_name,column_name FROM information_schema.columns WHERE table_schema IN ('knowledge_data','knowledge_retained') ORDER BY table_schema,table_name,ordinal_position").fetchall()
        actual={}
        for table,column in rows:actual.setdefault(table,[]).append(column)
        assert actual=={name:row['columns'] for name,row in inventory['relations'].items()}
        assert db.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('knowledge_data','knowledge_retained') AND c.relkind='r' AND c.relrowsecurity").fetchone()[0]==0
    assert inventory['activation'] is False
