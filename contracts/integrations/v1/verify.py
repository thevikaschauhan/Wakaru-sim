import hashlib,json
from pathlib import Path
p=Path(__file__).resolve().parent
for name,digest in json.loads((p/'manifest.json').read_text())['sha256'].items():
 assert hashlib.sha256((p/name).read_bytes()).hexdigest()==digest,name
print('integration contract manifest verified')
