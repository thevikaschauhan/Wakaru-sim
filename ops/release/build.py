"""Build a local forward overlay on an exact cached image. Never pulls/pushes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASE = 'wakaru-legacy-admission@sha256:8957c68cf3685f3bb82452730e360ba8371966d707446dd281517aa2af319572'
LOCAL_BASE = 'wakaru-legacy-admission:amd64-20261029'
ALLOWED_CHANGES = {
    'backend/app/api/cart_recovery.py',
    'backend/app/config.py',
    'backend/app/services/readiness.py',
    'backend/app/services/recovery_admission.py',
    'backend/Dockerfile.lean',
    'ops/runtime/lean-files.txt',
    'ops/runtime/inventory_legacy.py',
    'ops/runtime/quarantine_maintenance.py',
    'ops/release/profile.json',
}

PROBE = r'''
import hashlib,importlib.metadata,json
from pathlib import Path
root=Path('/app');manifest=root/'ops/runtime/lean-files.txt'
names={x for x in manifest.read_text().splitlines() if x and not x.startswith('#')}
names.add('ops/runtime/lean-files.txt')
files={n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in sorted(names)}
packages={d.metadata['Name'].lower():d.version for d in importlib.metadata.distributions()}
print(json.dumps({'files':files,'packages':packages},sort_keys=True))
'''


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


def image_state(image):
    state = json.loads(docker('image','inspect',image))[0]
    return {key:state[key] for key in ('Id','RepoDigests','Architecture','Os','Size','RootFS')}


def fingerprint(image):
    return json.loads(docker('run','--rm','--pull=never','--network=none','--read-only',
        '--entrypoint','python',image,'-c',PROBE))


def build(base_source, output, tag):
    output.mkdir(parents=True, exist_ok=True)
    state = image_state(LOCAL_BASE)
    assert BASE in state['RepoDigests'], 'base digest not present locally'
    assert image_state(LOCAL_BASE)['Id'] == state['Id'], 'local tag differs from pinned base'
    before = fingerprint(LOCAL_BASE)
    # Bind every base runtime file, not just the three files being backported.
    for name, digest in before['files'].items():
        assert hashlib.sha256((base_source/name).read_bytes()).hexdigest() == digest, 'base source mismatch: '+name
    names = {line for line in (ROOT/'ops/runtime/lean-files.txt').read_text().splitlines()
             if line and not line.startswith('#')}
    names.add('ops/runtime/lean-files.txt')
    expected = {}
    for name in sorted(names):
        path = Path(name)
        assert not path.is_absolute() and '..' not in path.parts
        assert not (ROOT/path).is_symlink() and (ROOT/path).is_file()
        expected[name] = hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
    assert set(before['files']) <= set(expected), 'forward must not remove retained runtime sources'
    changed = {name for name,digest in expected.items() if before['files'].get(name) != digest}
    assert changed == ALLOWED_CHANGES, ('unexpected forward surface',changed ^ ALLOWED_CHANGES)
    with tempfile.TemporaryDirectory(prefix='wakaru-rollback-build-') as directory:
        context = Path(directory)
        for name in sorted(changed):
            destination = context/'overlay'/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT/name,destination)
        dockerfile = (f'# Base identity is checked against {BASE} before and after build.\nFROM {LOCAL_BASE}\n'
            'COPY --chown=appuser:appuser overlay/ /app/\n'
            'ENV RECOVERY_ANALYSIS_MODE=legacy_simulation RECOVERY_DIRECT_MERCHANT_IDS="" TYPESAFE_RECOVERY_MODE=off\n'
            'LABEL org.vakaru.release.profile="simulation_retirement_forward_v1"\n')
        (context/'Dockerfile').write_text(dockerfile)
        (output/'Dockerfile').write_text(dockerfile)
        result = subprocess.run(['docker','build','--pull=false','--network=none','--provenance=false',
            '--tag',tag,str(context)],capture_output=True)
        (output/'build.log').write_bytes(result.stdout+result.stderr)
        if result.returncode: raise RuntimeError((result.stdout+result.stderr).decode()[-4000:])
    built = image_state(tag); after = fingerprint(tag)
    assert before['packages'] == after['packages'], 'dependency drift'
    assert after['files'] == expected, 'image/source mismatch'
    assert built['RootFS']['Layers'][:len(state['RootFS']['Layers'])] == state['RootFS']['Layers'], 'base layers changed'
    assert image_state(LOCAL_BASE)['Id'] == state['Id'], 'base identity changed'
    report = {'version':'simulation_retirement_forward_build_v1','base':state,'forward':built,
        'tag':tag,'base_source':str(base_source),'source':str(ROOT),'runtime_files':expected,
        'changed_runtime_files':sorted(changed),'packages':after['packages'],
        'release_sha256':hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'base_layers_preserved':True,'dependencies_identical':True,'production_access':False,
        'qualification':'pending'}
    (output/'build.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('tag','changed_runtime_files','dependencies_identical')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tag',default='wakaru-retirement-forward:amd64-20261029')
    args = parser.parse_args()
    build(args.base_source.resolve(),args.output.resolve(),args.tag)
