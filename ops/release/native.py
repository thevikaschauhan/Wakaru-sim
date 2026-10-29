"""Validate the exact forward/rollback pair on a native AMD64 host, offline."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE_MANIFEST = '426ea08eac3d3e8a534fd370dbb94c5d9851db8d05edeed56dd60894497e5421'
FORWARD_ARCHIVE = '2af4f4ca6b34a170112d273a59be35f46b00a7d08638dd098ff2bbb96c942cbf'


def helper(packet):
    spec = importlib.util.spec_from_file_location('rollback_native', packet/'base/ops/rollback/native.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(args):
    # The outer packet SHA is checked by the workflow before this script runs.
    packet=args.packet.resolve(); out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    native=helper(packet)
    result={'passed':False,'native_validation':'pending','production_access':False,'deployment':False}
    try:
        if native.sha(packet/'manifest.json')!=args.manifest_sha256:
            raise RuntimeError('packet manifest checksum mismatch')
        manifest=json.loads((packet/'manifest.json').read_text())
        for name,expected in manifest['files'].items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or (packet/path).is_symlink():
                raise RuntimeError('unsafe packet path')
            if native.sha(packet/path)!=expected:raise RuntimeError('packet file mismatch: '+name)
        if native.sha(packet/'base/manifest.json')!=BASE_MANIFEST:
            raise RuntimeError('original native packet changed')
        base=json.loads((packet/'base/manifest.json').read_text())
        native.verify_files(packet/'base',base)
        if native.sha(packet/'forward/wakaru-retirement-forward.image.tar.gz')!=FORWARD_ARCHIVE:
            raise RuntimeError('forward archive changed')
        # Refuse emulation BEFORE Docker loads or fixture mutations.
        result['host']=native.native_host()
        result['manifest_sha256']=args.manifest_sha256
        images={**base['images'],'forward':manifest['forward_tag']}
        bindings={**base['image_bindings'],images['forward']:manifest['forward_binding']}
        for tag,expected in bindings.items():
            exists=subprocess.run(['docker','image','inspect',tag],capture_output=True,env=native.ENV)
            if exists.returncode==0:native.verify_image(json.loads(exists.stdout)[0],expected)
        for archive in ('base/fixture-images.tar.gz','base/artifact/wakaru-retirement-rollback.image.tar.gz','forward/wakaru-retirement-forward.image.tar.gz'):
            native.run('docker','load','--input',str(packet/archive))
        states={role:native.inspect(tag) for role,tag in images.items()}
        for role,state in states.items():native.verify_image(state,bindings[images[role]])
        rollback=json.loads((packet/'base/artifact/build.json').read_text())
        forward=json.loads((packet/'forward/build.json').read_text())
        for role in ('base','rollback','current'):rollback[role]['Id']=states[role]['Id']
        forward['forward']['Id']=states['forward']['Id']
        (out/'rollback').mkdir(); (out/'forward').mkdir()
        native.write(out/'rollback/build.json',rollback);native.write(out/'forward/build.json',forward)
        subprocess.run([sys.executable,str(packet/'ops/release/admission_matrix.py'),
            '--artifact',str(out/'forward'),'--output',str(out/'admission-matrix.json')],env=native.ENV,check=True)
        subprocess.run([sys.executable,str(packet/'ops/release/qualify.py'),
            '--artifact',str(out/'rollback'),'--forward-artifact',str(out/'forward'),
            '--output',str(out/'qualification'),'--engine',str(packet/'base/engine'),
            '--engine-binaries',str(packet/'base/engine/binaries'),
            '--redis-image',states['redis']['Id'],'--postgres-image',states['postgres']['Id']],env=native.ENV,check=True)
        matrix=json.loads((out/'admission-matrix.json').read_text())
        qualification=json.loads((out/'qualification/result.json').read_text())
        assert matrix['passed'] and len(matrix['checks'])==17 and all(c['passed'] for c in matrix['checks'])
        assert qualification['passed'] and qualification['native_amd64'] and len(qualification['checks'])==20
        assert all(c['passed'] for c in qualification['checks'])
        result.update(passed=True,native_validation='passed',forward_archive_sha256=FORWARD_ARCHIVE,
            rollback_archive_sha256=base['original_archive_sha256'],
            host_image_ids={role:s['Id'] for role,s in states.items()},
            qualification_checks=qualification['checks'],configuration_checks=matrix['checks'],
            qualification_sha256=native.sha(out/'qualification/result.json'))
    except Exception as error:
        result.update(native_validation='not_passed',error=str(error))
        raise
    finally:
        native.write(out/'native-result.json',result)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--packet',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--manifest-sha256',required=True)
    validate(parser.parse_args())
