"""Offline durable-cleanup source/schema binding; no runtime activation."""
import hashlib
import json
from pathlib import Path
import sys

here=Path(__file__).resolve().parent
root=here.parents[2]
manifest=json.loads((here/'manifest.json').read_text())
for name,want in manifest['wakaru_sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==want,name
scope=json.loads((root/'contracts/knowledge/v1/schema.json').read_text())['$defs']['scope']
assert json.loads((here/'request.schema.json').read_text())['properties']['scope']==scope
if len(sys.argv)>1:
    engine=Path(sys.argv[1])
    for name,want in manifest['engine_sha256'].items():
        assert hashlib.sha256((engine/name).read_bytes()).hexdigest()==want,name
    for name in ('request','response'):
        assert (here/(name+'.schema.json')).read_bytes()==(engine/'internal/knowledgeclient'/('cleanup_'+name+'.schema.json')).read_bytes()
print(json.dumps({'bound':True,'runtime_enabled':False,'privacy_completion_qualified':False,'cross_repository_verified':len(sys.argv)>1}))
