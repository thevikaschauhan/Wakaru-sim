"""Review tripwires must retain upstream blockers and detect new writers."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('combined_privacy',ROOT/'ops/runtime/verify_privacy.py')
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


def snapshot(tmp_path):
    for name in ['backend','cart_recovery','contracts/integrations/c14']:
        shutil.copytree(ROOT/name,tmp_path/name,ignore=shutil.ignore_patterns('__pycache__','.venv','uploads'))
    return tmp_path


def test_inventory_preserves_upstream_privacy_limits():
    result = privacy.verify(ROOT)
    assert result['base_blockers_retained'] == 18
    assert result['new_blocked_families'] == 1
    assert result['whole_application_erasure_complete'] is False
    assert result['cross_repository_parity_qualified'] is False


@pytest.mark.parametrize('change',['writer','changed','migration','blocker'])
def test_unreviewed_persistence_and_weakened_gates_fail(tmp_path,change):
    root = snapshot(tmp_path)
    if change == 'writer':
        (root/'cart_recovery/unreviewed.py').write_text('import sqlite3\nconnection = sqlite3.connect("new.sqlite")\n')
    elif change == 'changed':
        with (root/'cart_recovery/adaptive/journal.py').open('a') as f:f.write('\n# changed writer\n')
    elif change == 'migration':
        (root/'cart_recovery/adaptive/migrations/003_new.sql').write_text('CREATE TABLE unknown(payload text);')
    else:
        p=root/'contracts/integrations/c14/wakaru-runtime-addendum.json'
        value=json.loads(p.read_text());value['retained_base_blockers'].pop();p.write_text(json.dumps(value))
    with pytest.raises(ValueError):privacy.verify(root)


def test_unreviewed_knowledge_migration_fails(tmp_path):
    root = snapshot(tmp_path)
    (root/'cart_recovery/domain/migrations/002_unreviewed.sql').write_text('CREATE TABLE unreviewed(payload jsonb);')
    with pytest.raises(ValueError,match='knowledge_migration_drift'):
        privacy.verify(root)
