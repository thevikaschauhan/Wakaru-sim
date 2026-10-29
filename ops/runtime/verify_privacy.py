"""Wakaru-only source inventory addendum. Never claims cross-service erasure."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
WRITER = re.compile(r'\b(?:INSERT\s+INTO|UPDATE\s+[a-z_]|DELETE\s+FROM|CREATE\s+TABLE)\b|\b(?:write_text|write_bytes|sqlite3\.connect|enqueue|hset|sadd|json\.dump)\b', re.I)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def writers(root):
    result = {}
    for directory in ('backend', 'cart_recovery'):
        for path in (root/directory).rglob('*.py'):
            relative = path.relative_to(root)
            if any(p in {'tests','scripts','.venv','__pycache__'} for p in relative.parts) or path.name.startswith('test_'):
                continue
            if WRITER.search(path.read_text()):
                result[str(relative)] = digest(path)
    return result


def verify(root=ROOT):
    root = Path(root)
    base_path = root/'contracts/integrations/c14/inventory.json'
    value = json.loads((root/'contracts/integrations/c14/wakaru-runtime-addendum.json').read_text())
    base = json.loads(base_path.read_text())
    if value['base_sha256'] != digest(base_path):
        raise ValueError('base_inventory_drift')
    if value['scope'] != 'wakaru_combined_source_only' or value['whole_application_erasure_complete'] is not False:
        raise ValueError('inventory_scope')
    blocked = sorted(f['id'] for f in base['families'] if f['status'] in {'legacy_blocked','external_blocked'})
    if value['retained_base_blockers'] != blocked:
        raise ValueError('base_blocker_removed')
    families = {f['id']:f for f in base['families']}
    for f in value['additional_families']:
        if f['id'] in families or f['status'] != 'legacy_blocked':
            raise ValueError('unqualified_family')
        for key in ('owner','subject_selector','export','erasure','retention','restore','blocker'):
            if not f.get(key):
                raise ValueError('missing_lifecycle')
        families[f['id']] = f
    if any(row['family'] not in families for row in value['writers'].values()):
        raise ValueError('unclassified_writer')
    if writers(root) != {p:r['sha256'] for p,r in value['writers'].items()}:
        raise ValueError('writer_inventory_drift')
    migrations = {str(p.relative_to(root)):digest(p) for p in (root/'cart_recovery/adaptive/migrations').glob('*.sql')}
    if migrations != value['adaptive_migrations']:
        raise ValueError('adaptive_migration_drift')
    for name, expected in value['lineage_controls'].items():
        if digest(root/name) != expected:
            raise ValueError('lineage_control_drift')
    return {'scope':value['scope'],'writers':len(value['writers']),
            'base_blockers_retained':len(blocked),'new_blocked_families':len(value['additional_families']),
            'whole_application_erasure_complete':False,'cross_repository_parity_qualified':False}


if __name__ == '__main__':
    print(json.dumps(verify(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT),sort_keys=True))
