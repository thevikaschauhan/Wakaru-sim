"""The combined packaged service must bind all shipped code and dependencies."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_packaged_release_hash_binds_runtime_and_split_dependencies(tmp_path):
    source = Path(__file__).resolve().parents[2]
    artifact = tmp_path / 'runtime'
    subprocess.run([sys.executable, str(source/'ops/runtime/build.py'), str(source), str(artifact)], check=True)
    code = r'''
from pathlib import Path
from cart_recovery.adaptive import promotion
first = promotion.artifact_hash()
assert len(first) == 64
for name in ['backend/requirements-core.txt', 'backend/app/config.py', 'contracts/recovery/v2/judgment-catalog.json']:
    path = Path(name)
    original = path.read_bytes()
    path.write_bytes(original+b'\n')
    promotion.artifact_hash.cache_clear()
    assert promotion.artifact_hash() != first, name
    path.write_bytes(original)
    promotion.artifact_hash.cache_clear()
    assert promotion.artifact_hash() == first
print(first)
'''
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONPATH':str(artifact)}
    result = subprocess.run([sys.executable,'-c',code],cwd=artifact,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
