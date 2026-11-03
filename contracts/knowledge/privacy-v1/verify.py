"""Offline source and cross-language schema binding. No runtime activation."""
import hashlib
import json
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[3]
here=Path(__file__).resolve().parent
manifest=json.loads((here/'manifest.json').read_text())
for path,expected in manifest['wakaru_sha256'].items():
    assert hashlib.sha256((root/path).read_bytes()).hexdigest()==expected,path
request=json.loads((here/'request.schema.json').read_text())
domain=json.loads((root/'contracts/knowledge/v1/schema.json').read_text())
assert request['$defs']['scope']==domain['$defs']['scope']
if len(sys.argv)>1:
    engine=Path(sys.argv[1])
    for path,expected in manifest['engine_sha256'].items():
        assert hashlib.sha256((engine/path).read_bytes()).hexdigest()==expected,path
    for name in ('request','response'):
        assert (here/(name+'.schema.json')).read_bytes()==(engine/'internal/knowledgeclient'/('privacy_'+name+'.schema.json')).read_bytes()
print(json.dumps({'bound':True,'runtime_enabled':False,'privacy_completion_qualified':False,'cross_repository_verified':len(sys.argv)>1}))
