import json
import sqlite3
import pytest
from cart_recovery.integration_privacy import PrivacyStore, PrivacyStop


def task(operation='erase', generation=2, request='request-a', scope='subject'):
    return dict(version='integration_privacy_task_v1', merchant_id='merchant-a', store_id='store-a', request_id=request, subject_key='a'*64, privacy_generation=generation, operation=operation, scope=scope, source_keys=['b'*64], inventory_revision='integration_c5_v1', live_ingestion_enabled=False, email_use_enabled=False)


def put(store, key='artifact-a', subject='a'*64, sources=None, generation=1, kind='result'):
    store.put('merchant-a','store-a',artifact_id=key,subject=subject,generation=generation,kind=kind,sources=sources or ['b'*64],payload={'private':'fixture result'},expires=200,now=100)


def test_export_erase_replay_restart_and_restore(tmp_path):
    path=tmp_path/'privacy.sqlite'; store=PrivacyStore(path)
    for kind in ['input_snapshot','journal','result','plan','judgment','pending_job']:
        put(store,key=kind,kind=kind)
    receipt, output=store.apply('merchant-a','store-a',json.dumps(task('export',1,'export-a')),now=101)
    assert len(output)==6 and receipt['service']=='wakaru'
    raw=json.dumps(task())
    receipt,output=store.apply('merchant-a','store-a',raw,now=102)
    assert not output and receipt['status']=='complete'
    store=PrivacyStore(path)
    assert store.apply('merchant-a','store-a',raw,now=103)[0]==receipt
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM privacy_artifacts').fetchone()[0]==0
        # Simulate stale artifact restoration while preserving the deletion ledger.
        db.execute('INSERT INTO privacy_artifacts VALUES(?,?,?,?,?,?,?,?,?)',('merchant-a','store-a','restored','a'*64,2,'result',json.dumps(['b'*64]),json.dumps({'private':'stale'}),200))
    with pytest.raises(PrivacyStop): store.read('merchant-a','store-a','restored',now=104)
    with pytest.raises(PrivacyStop): put(store,generation=2)
    with pytest.raises(PrivacyStop): store.apply('merchant-a','store-a',json.dumps(task('export',1,'export-a')),now=104)


def test_stop_commits_before_failure_and_retries(tmp_path):
    path=tmp_path/'privacy.sqlite'; store=PrivacyStore(path);put(store)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON privacy_artifacts BEGIN SELECT RAISE(FAIL,'fixture failure'); END")
    with pytest.raises(sqlite3.DatabaseError): store.apply('merchant-a','store-a',json.dumps(task()),now=101)
    with pytest.raises(PrivacyStop): store.read('merchant-a','store-a','artifact-a',now=101)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT complete FROM privacy_requests').fetchone()[0]==0
        db.execute('DROP TRIGGER fail_delete')
    assert store.apply('merchant-a','store-a',json.dumps(task()),now=102)[0]['status']=='complete'


def test_expiry_is_revision_scoped_and_tenant_bound(tmp_path):
    store=PrivacyStore(tmp_path/'privacy.sqlite');put(store)
    with pytest.raises(PrivacyStop): store.read('merchant-a','store-a','artifact-a',now=200)
    raw=json.dumps(task('expire'))
    with pytest.raises(ValueError): store.apply('merchant-b','store-a',raw,now=201)
    store.apply('merchant-a','store-a',raw,now=201)
    with pytest.raises(PrivacyStop): put(store,generation=2)
    put(store,key='new-revision',sources=['c'*64],generation=2)
    assert store.read('merchant-a','store-a','new-revision',now=101)['private']=='fixture result'
    changed=task('expire');changed['source_keys']=['d'*64]
    with pytest.raises(PrivacyStop): store.apply('merchant-a','store-a',json.dumps(changed),now=202)


def test_physical_retention_withdraws_related_results(tmp_path):
    path=tmp_path/'privacy.sqlite'; store=PrivacyStore(path);put(store,key='journal',kind='journal')
    store.put('merchant-a','store-a',artifact_id='result',subject='a'*64,generation=1,kind='result',sources=['b'*64],payload={'private':'derived'},expires=300,now=100)
    assert store.purge_expired('merchant-a','store-a',now=200)==2
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM privacy_artifacts').fetchone()[0]==0
    with pytest.raises(PrivacyStop): put(store,key='replay')
