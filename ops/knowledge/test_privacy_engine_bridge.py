"""Real compiled Go client -> loopback WSGI -> disposable PostgreSQL.

Run with tests/knowledge so its disposable database fixtures are available.
Requires an explicit binary built from the pinned Engine checkout; no skips.
"""
import json
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server, WSGIRequestHandler

from test_privacy_service import privacy_service, export_store, store
from conftest import database, cluster


class Quiet(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


@pytest.fixture
def bridge(privacy_service):
    app,store,f = privacy_service
    binary = os.environ.get('KNOWLEDGE_PRIVACY_FIXTURE_BIN')
    assert binary and Path(binary).is_file(), 'explicit compiled privacy fixture binary required'
    state = {'lose_ack':False,'calls':0}
    def wrapped(environ,start_response):
        state['calls']+=1
        captured=[]
        def capture(status,headers,exc_info=None):
            captured.extend((status,headers))
        result=app(environ,capture)
        try:body=b''.join(result)
        finally:
            if hasattr(result,'close'):result.close()
        if state['lose_ack'] and environ['PATH_INFO'].endswith('/purge_withdrawn') and captured[0].startswith('200'):
            start_response('503 Service Unavailable',[('Content-Type','application/json')])
            return [b'{"error":"fixture_lost_ack"}']
        start_response(captured[0],captured[1])
        return [body]
    server=make_server('127.0.0.1',0,wrapped,request_handler=Quiet)
    thread=Thread(target=server.serve_forever);thread.start()
    def call(operation,scope=None,**args):
        return subprocess.run([binary,f'http://127.0.0.1:{server.server_port}'],input=json.dumps(dict(operation=operation,scope=scope or f['bindings']['scope'],**args)),text=True,capture_output=True,timeout=25)
    try:yield call,store,f,state
    finally:
        server.shutdown();thread.join(timeout=5);server.server_close()
        assert not thread.is_alive()


def test_engine_privacy_cleanup_and_paginated_after_erasure_export(bridge):
    call,store,f,state=bridge;scope=f['bindings']['scope']
    result=call('purge_withdrawn',expected_version=2)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==dict(version=3,removed_sources=1,removed_objects=1,whole_application_complete=False)
    assert store.export_subject(scope)['payload'] is None
    assert call('purge_withdrawn',expected_version=2).returncode==2
    for i in range(105):store.withdraw_source(scope,dict(source_id=f'pending-{i}',revision=1))
    store.erase_subject(scope)
    result=call('export_retained',max_rows=1000)
    assert result.returncode==0,result.stderr
    pages=json.loads(result.stdout)
    assert len(pages)>3 and len({p['snapshot'] for p in pages})==1
    assert all(p['subject']['erased'] and not p['whole_application_complete'] for p in pages)
    for family in ['withdrawals','delivery_receipts','events']:
        rows=[row for p in pages if p['family']==family for row in p['rows']]
        assert len(rows)==pages[0]['counts'][family]
    result=call('export_retained',max_rows=10)
    assert result.returncode==2 and result.stdout==''
    assert call('export_retained',scope=dict(scope,privacy_generation=2),max_rows=1000).returncode==2


def test_engine_privacy_lost_cleanup_ack_is_unknown_without_retry(bridge):
    call,store,f,state=bridge;scope=f['bindings']['scope'];state['lose_ack']=True
    result=call('purge_withdrawn',expected_version=2)
    assert result.returncode==2 and result.stdout==''
    assert result.stderr.strip()=='knowledge_outcome_unknown'
    assert state['calls']==1
    assert store.export_subject(scope)['sources']==[]
    assert store.current(scope)['version']==3
    # An independently initiated stale request cannot perform cleanup again.
    state['lose_ack']=False
    assert call('purge_withdrawn',expected_version=2).returncode==2
    assert store.current(scope)['version']==3


def test_engine_privacy_scope_and_quarantine_refusal(bridge):
    call,store,f,state=bridge;scope=f['bindings']['scope']
    wrong=dict(scope,subject_id='other-shopper')
    assert call('purge_withdrawn',scope=wrong,expected_version=2).returncode==2
    assert len(store.export_subject(scope)['sources'])==1
    store.quarantine()
    assert call('purge_withdrawn',expected_version=2).returncode==2
    assert call('export_retained',max_rows=1000).returncode==2
