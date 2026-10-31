"""Offline content binding for the disabled-by-default knowledge service slice."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
manifest=json.loads((HERE/'manifest.json').read_text())
for path,expected in manifest['sha256'].items():
    assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==expected,path
schema=json.loads((HERE/'request.schema.json').read_text())
domain=json.loads((ROOT/'contracts/knowledge/v1/schema.json').read_text())
assert all(schema['$defs'][name]==value for name,value in domain['$defs'].items())
if len(sys.argv)>1:
    engine=Path(sys.argv[1])
    for path,expected in manifest['engine_sha256'].items():
        assert hashlib.sha256((engine/path).read_bytes()).hexdigest()==expected,path
print(json.dumps({'bound':True,'runtime_enabled':False,'cross_repository_source_verified':len(sys.argv)>1}))
