"""Verify version-pinned contracts without fetching a schema at runtime."""
import hashlib
import json
from pathlib import Path
root=Path(__file__).resolve().parent
manifest=json.loads((root/'manifest.json').read_text())
for name,expected in manifest['sha256'].items():
    actual=hashlib.sha256((root/name).read_bytes()).hexdigest()
    if actual!=expected:raise SystemExit('intelligence contract drift: '+name)
print('intelligence_v1 contract hashes verified')
