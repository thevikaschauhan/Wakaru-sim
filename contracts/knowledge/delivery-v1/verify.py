"""Source binding only; it does not enable delivery or qualify packaged artifacts."""
import hashlib
import json
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[3]
manifest=json.loads((Path(__file__).parent/'manifest.json').read_text())
if len(sys.argv)!=2:
    raise SystemExit('explicit companion Engine checkout required')
engine=Path(sys.argv[1])
for directory,key in ((root,'wakaru_sha256'),(engine,'engine_sha256')):
    for path,expected in manifest[key].items():
        if hashlib.sha256((directory/path).read_bytes()).hexdigest()!=expected:
            raise SystemExit('source binding mismatch: '+path)
assert manifest['runtime_enabled'] is False
assert manifest['source_producers_wired'] is False
assert manifest['cross_repository_privacy_qualified'] is False
print(json.dumps({'bound':True,'runtime_enabled':False,'scope_control_only':True,'cross_repository_source_verified':True}))
