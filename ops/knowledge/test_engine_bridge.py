"""Explicit cross-repo qualification; requires a built Engine fixture, never skips."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest
from werkzeug.serving import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests/knowledge'))
from conftest import cluster, database  # noqa: F401
from test_service import GRANT, KEY, data, command
from cart_recovery.domain.store import KnowledgeStore
from cart_recovery.domain.service import create_knowledge_service_app


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


@pytest.fixture
def endpoint(database):
    store=KnowledgeStore(database,KEY);store.bootstrap_empty()
    app=create_knowledge_service_app(enabled=True,grants=[GRANT],store=store)
    server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',store
    finally:server.shutdown();server.server_close();thread.join(timeout=5);assert not thread.is_alive()


def call(endpoint,value,operation,args=None,expected=0):
    binary=os.environ.get('KNOWLEDGE_ENGINE_FIXTURE')
    assert binary and Path(binary).is_file(),'built Engine fixture required; no skipped proof'
    result=subprocess.run([binary,endpoint[0]],input=json.dumps(command(value,operation,args)),
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==expected,result.stderr
    return json.loads(result.stdout) if result.returncode==0 else result.stderr


def seed(endpoint,value):
    call(endpoint,value,'provision')
    for receipt in value['bindings']['sources']:
        raw=json.dumps(receipt['claims']).encode()
        call(endpoint,value,'source',dict(receipt=receipt,content_base64=base64.b64encode(raw).decode()))
    for receipt in value['bindings']['entities']:call(endpoint,value,'entity',dict(receipt=receipt))


def test_engine_source_memory_restart_export_and_erasure(endpoint,database):
    value=data();seed(endpoint,value)
    args=dict(payload=value['payload'],expected_version=0,command_id='engine-command')
    first=call(endpoint,value,'save',args);assert first['result']=={'version':1,'replayed':False}
    assert call(endpoint,value,'save',args)['result']=={'version':1,'replayed':True}
    # Each call is a new Engine OS process; a new store client sees durable data.
    assert KnowledgeStore(database,KEY).current(value['bindings']['scope'])['objects']==value['payload']['objects']
    assert call(endpoint,value,'current')['result']['objects']==value['payload']['objects']
    assert call(endpoint,value,'export_subject')['result']['payload']==value['payload']
    call(endpoint,value,'erase_subject');checkpoint=endpoint[1].retained_checkpoint()
    call(endpoint,value,'erase_subject');assert checkpoint==endpoint[1].retained_checkpoint()
    assert '409' in call(endpoint,value,'save',args,expected=1)
    assert '409' in call(endpoint,value,'current',expected=1)


def test_engine_withdrawal_and_identity_change(endpoint):
    value=data();seed(endpoint,value)
    call(endpoint,value,'save',dict(payload=value['payload'],expected_version=0,command_id='first'))
    source=value['bindings']['sources'][0];ref={k:source[k] for k in ['source_id','revision']}
    assert call(endpoint,value,'withdraw_source',{'source_ref':ref})['result']=={'version':2}
    assert call(endpoint,value,'current')['result']['objects']==[]
    new={**value['bindings']['scope'],'identity_revision':2,'privacy_generation':2}
    call(endpoint,value,'revise_identity',{'new_scope':new})
    assert '409' in call(endpoint,value,'erase_subject',expected=1)
    value['bindings']['scope']=new
    assert call(endpoint,value,'current')['result']['version']==3


def test_engine_cannot_provision_another_store(endpoint):
    value=data();value['bindings']['scope']['store_id']='2'
    assert 'binding_invalid' in call(endpoint,value,'provision',expected=1)
    assert endpoint[1].retained_checkpoint()['subjects']==[]


def test_engine_cannot_lift_restore_quarantine(endpoint):
    endpoint[1].quarantine()
    assert '409' in call(endpoint,data(),'provision',expected=1)
    assert 'binding_invalid' in call(endpoint,data(),'bootstrap_empty',expected=1)
