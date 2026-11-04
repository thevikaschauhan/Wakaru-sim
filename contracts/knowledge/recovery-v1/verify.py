"""Offline source/schema binding for cleanup export and recovery."""
import hashlib
import json
from pathlib import Path
import sys

here=Path(__file__).resolve().parent
root=here.parents[2]
manifest=json.loads((here/'manifest.json').read_text())
for name,want in manifest['wakaru_sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==want,name
schema=json.loads((here/'checkpoint.schema.json').read_text())
assert schema['properties']['fences']==json.loads((root/'contracts/knowledge/lifecycle-v1/fences.schema.json').read_text())
result=json.loads((root/'contracts/knowledge/cleanup-v1/response.schema.json').read_text())['properties']['result']['oneOf'][1]
assert schema['properties']['cleanup_receipts']['items']['prefixItems'][-1]==result
if len(sys.argv)>1:
    engine=Path(sys.argv[1])
    for name,want in manifest['engine_sha256'].items():
        assert hashlib.sha256((engine/name).read_bytes()).hexdigest()==want,name
    for name in ('request','response'):
        assert (root/'contracts/knowledge/privacy-v1'/(name+'.schema.json')).read_bytes()==(engine/'internal/knowledgeclient'/('privacy_'+name+'.schema.json')).read_bytes()
print(json.dumps({'bound':True,'runtime_enabled':False,'privacy_completion_qualified':False,'cross_repository_verified':len(sys.argv)>1}))
