"""Local fixture only: commit mutations, deliberately lose HTTP acknowledgments."""
import sys
from pathlib import Path
from urllib.parse import urlsplit
import psycopg
from werkzeug.serving import make_server, WSGIRequestHandler
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from cart_recovery.domain.delivery import create_delivery_app, AUDIENCE, PREFIX, OPERATIONS
from cart_recovery.domain.store import KnowledgeStore

if len(sys.argv)!=4 or urlsplit(sys.argv[1]).hostname!='127.0.0.1' or 'test' not in urlsplit(sys.argv[1]).path:
    raise SystemExit('disposable loopback test DSN required')
dsn,merchant,store_id=sys.argv[1:]
store=KnowledgeStore(dsn,b'fixture-only-delivery-fence-key-32-bytes')
with psycopg.connect(dsn) as db:
    empty=db.execute('SELECT key_fingerprint IS NULL FROM knowledge_retained.control WHERE singleton').fetchone()[0]
if empty:store.bootstrap_empty()
grant=dict(key_id='fixture',secret='fixture-only-delivery-key-32-bytes-long',merchant_id=merchant,store_id=store_id,audience=AUDIENCE,operations=['POST '+PREFIX+op for op in (*OPERATIONS,'receipt')])
app=create_delivery_app(enabled=True,grants=[grant],store=store)


def lose_ack(environ,start_response):
    captured=[]
    def capture(status,headers,exc_info=None):captured.extend((status,headers))
    response=app(environ,capture)
    try:body=b''.join(response)
    finally:
        if hasattr(response,'close'):response.close()
    if not environ['PATH_INFO'].endswith('/receipt') and captured[0].startswith('200'):
        start_response('503 Service Unavailable',[('Content-Type','application/json')]);return [b'{"error":"fixture_lost_ack"}']
    start_response(captured[0],captured[1]);return [body]


class Quiet(WSGIRequestHandler):
    def log_request(self,*args,**kwargs):pass

server=make_server('127.0.0.1',0,lose_ack,threaded=True,request_handler=Quiet)
print(f'http://127.0.0.1:{server.server_port}',flush=True)
server.serve_forever()
