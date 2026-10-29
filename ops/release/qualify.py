"""Qualify the built rollback image using only owned, disposable local services."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
CURRENT = 'wakaru-legacy-admission@sha256:35a659feda7a7807a9d85117cc31e8ab1a362318073434a331b8fc0e1b4dbcbc'
ENV = {key:os.environ[key] for key in ('HOME','PATH') if key in os.environ}
PILOT = '11111111-1111-4111-8111-111111111111'


def run(args, required=True, **kw):
    result = subprocess.run(args,env=kw.pop('env',ENV),capture_output=True,timeout=kw.pop('timeout',180),**kw)
    if required and result.returncode: raise RuntimeError((result.stdout+result.stderr).decode()[-6000:])
    return result


def docker(*args, **kw): return run(['docker',*args],**kw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--forward-artifact',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--engine',type=Path,required=True)
    parser.add_argument('--engine-binaries',type=Path)
    parser.add_argument('--current-image',default=CURRENT)
    parser.add_argument('--redis-image',default='redis:8.2.9')
    parser.add_argument('--postgres-image',default='postgres:18')
    args = parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    build=json.loads((args.artifact/'build.json').read_text());image=build['rollback']['Id']
    forward=json.loads((args.forward_artifact/'build.json').read_text())
    args.current_image=forward['tag']
    assert json.loads(docker('image','inspect',args.current_image).stdout)[0]['Id']==forward['forward']['Id']
    prefix='retirement-artifact-'+uuid.uuid4().hex[:10];network=prefix;volume=prefix+'-data'
    containers=[];report={'production_access':False,'deployment':False,'image':image,'checks':[]}
    settings={'SECRET_KEY':'fixture-secret','WAKARU_API_KEY':'fixture-key',
        'WAKARU_INTERNAL_SECRET':'fixture-hmac','REDIS_URL':'redis://redis:6379/0',
        'TYPESAFE_RECOVERY_MODE':'off','SENTRY_DSN':'','CART_RECOVERY_RATE_LIMIT_PER_MIN':'1000',
        'PYTHONDONTWRITEBYTECODE':'1','ROLLBACK_RELEASE_SHA256':build['release_sha256']}
    def start(role, artifact, command=(), extra=None, port=False):
        name=prefix+'-'+role
        flags=['run','--pull=never','-d','--name',name,'--network',network,'--network-alias',role,
               '--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,nosuid,size=64m']
        if port: flags+=['-p','127.0.0.1::5001']
        for key,value in {**settings,**(extra or {})}.items():flags+=['-e',key+'='+value]
        docker(*flags,artifact,*command);containers.append(name);return name
    def stop(name):
        docker('stop','-t','15',name)
        (out/(name.removeprefix(prefix+'-')+'.log')).write_bytes(docker('logs',name).stdout+docker('logs',name).stderr)
        docker('rm','-v',name);containers.remove(name)
    def probe(action, role, suffix='', forward_image=False):
        name=prefix+'-probe-'+action+suffix
        flags=['run','--rm','--pull=never','--name',name,'--network',network,'--read-only',
            '--cap-drop=ALL','--security-opt=no-new-privileges','--entrypoint','python',
            '-v',str(ROOT/'ops/release/probe.py')+':/fixture/probe.py:ro']
        probe_settings={**settings,'ROLLBACK_RELEASE_SHA256':forward['release_sha256']} if forward_image else settings
        for key,value in probe_settings.items():flags+=['-e',key+'='+value]
        result=docker(*flags,args.current_image if forward_image else image,'/fixture/probe.py',action,'http://'+role+':5001')
        (out/(action+suffix+'-probe.log')).write_bytes(result.stdout+result.stderr)
        parsed=json.loads(result.stdout.splitlines()[-1]);assert parsed['passed'] and parsed['action']==action;parsed['phase']=suffix or 'rollback';report['checks'].append(parsed)
        (out/'progress.json').write_text(json.dumps(report,indent=2)+'\n')
    try:
        docker('network','create','--internal',network);docker('volume','create',volume)
        assert json.loads(docker('network','inspect',network).stdout)[0]['Internal'] is True
        redis=prefix+'-redis'
        docker('run','--pull=never','-d','--name',redis,'--network',network,'--network-alias','redis',
               '-v',volume+':/data',args.redis_image,'--appendonly','no','--save','')
        containers.append(redis)
        for _ in range(100):
            if docker('exec',redis,'redis-cli','ping',required=False).stdout.strip()==b'PONG':break
            time.sleep(.1)
        else:raise RuntimeError('fixture Redis did not start')
        probe('package_checks','unused')
        probe('package_checks','unused','-forward',forward_image=True)
        # Original lean image cannot preserve pilot-only mode without the
        # rollback startup adjustment. Prove that baseline without network.
        baseline=docker('run','--rm','--pull=never','--network=none','--entrypoint','python',
            '-e','RECOVERY_ANALYSIS_MODE=legacy_simulation',
            build['base']['Id'],'-c',"from app.config import Config; errors=Config.validate(); assert any('lean runtime' in e for e in errors); print('expected baseline startup failure confirmed')")
        (out/'baseline-startup.txt').write_bytes(baseline.stdout)
        report['checks'].append({'action':'baseline_startup_failure','passed':True})
        # Exercise actual default web and worker boot commands under a forbidden
        # global mode. Both must exit before serving/consuming.
        for role,command in [('web',[]),('worker',['python','worker.py'])]:
            flags=['run','--rm','--pull=never','--network=none']
            for key,value in {**settings,'RECOVERY_ANALYSIS_MODE':'direct_v1'}.items():flags+=['-e',key+'='+value]
            result=docker(*flags,image,*command,required=False,timeout=30)
            assert result.returncode and b'rollback requires legacy_simulation denial default' in result.stdout+result.stderr
            (out/('rejected-global-mode-'+role+'.log')).write_bytes(result.stdout+result.stderr)
            report['checks'].append({'action':'global_mode_rejected_'+role,'passed':True})
        default=start('default-web',image)
        probe('default_denies','default-web');stop(default)
        current_web=start('current-web',args.current_image,extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('denied','current-web','-forward')
        current_worker=start('current-worker',args.current_image,['python','worker.py'],extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('seed_current','current-web')
        stop(current_worker)
        probe('stage_pending','current-web')
        stop(current_web)
        rollback_web=start('rollback-web',image,extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('denied','rollback-web')
        rollback_worker=start('rollback-worker',image,['python','worker.py'],extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('rollback_drained','rollback-web')
        # Same Engine client and durable PostgreSQL fixture used by the last
        # slice, now targeting the packaged rollback server over real HTTP.
        engine=args.engine.resolve();pg=prefix+'-pg'
        docker('run','--pull=never','-d','--name',pg,'--network',network,
               '-e','POSTGRES_PASSWORD=fixture-only','-e','POSTGRES_DB=retirement_test',args.postgres_image)
        containers.append(pg)
        for _ in range(100):
            if docker('exec',pg,'pg_isready','-U','postgres',required=False).returncode==0 and docker('exec',pg,'cat','/proc/1/comm').stdout.strip()==b'postgres':break
            time.sleep(.1)
        else:raise RuntimeError('fixture PostgreSQL did not start')
        dsn='postgresql://postgres:fixture-only@127.0.0.1:5432/retirement_test?sslmode=disable'
        env={**ENV,'GOPROXY':'off','GOWORK':'off','GOCACHE':'/private/tmp/wakaru-retirement-go-cache',
             'GOOS':'linux','GOARCH':build['rollback']['Architecture'],'CGO_ENABLED':'0'}
        def sql(text):docker('exec','-i',pg,'psql','-Xq','-v','ON_ERROR_STOP=1','-U','postgres','-d','retirement_test',input=text.encode())
        sql('CREATE ROLE anon;CREATE ROLE authenticated;CREATE ROLE service_role;CREATE TABLE migrations(version varchar(255) PRIMARY KEY,description text,applied_at timestamptz DEFAULT now());')
        if args.engine_binaries:
            for name in ('uuid-migration','engine-contract'):
                shutil.copy2(args.engine_binaries/name,out/name)
                (out/name).chmod(0o755)
        else:
            run(['go','build','-o',str(out/'uuid-migration'),'./cmd/migrate-merchants-uuid'],cwd=engine,env=env)
        for path in sorted((engine/'migrations').glob('[0-9]*.sql')):
            if 'rollback' in path.name or int(path.name[:3])>66:continue
            if path.name.startswith('026_'):
                docker('run','--rm','--pull=never','--network','container:'+pg,'--read-only',
                    '-v',str(out/'uuid-migration')+':/fixture/uuid-migration:ro',
                    '-v',str(engine/'migrations')+':/fixture/migrations:ro','-w','/fixture',
                    '-e','DATABASE_URL='+dsn,'--entrypoint','/fixture/uuid-migration',image,'-env=scratch')
            else:
                sql(path.read_text());sql(f"INSERT INTO migrations(version,description) VALUES('{path.stem}','rollback artifact fixture') ON CONFLICT DO NOTHING")
        # Extend only the fixture hostname allowlist for this Docker-internal
        # service name. Engine production code and behavior assertions stay exact.
        if not args.engine_binaries:
            fixture=engine/'services/legacy_retirement_fixture_test.go'
            original=fixture.read_text()
            assert 'u.Hostname() != "127.0.0.1"' in original
            adjusted=out/'engine-container-fixture.go'
            adjusted.write_text(original.replace('u.Hostname() != "127.0.0.1"',
                '(u.Hostname() != "127.0.0.1" && u.Hostname() != "rollback-web")'))
            overlay=out/'engine-container-overlay.json'
            overlay.write_text(json.dumps({'Replace':{str(fixture):str(adjusted)}}))
            run(['go','test','-c','-tags=retirement_fixture','-o',str(out/'engine-contract'),'./services'],
                cwd=engine,env={**env,'GOFLAGS':'-overlay='+str(overlay)})
        result=docker('run','--rm','--pull=never','--network','container:'+pg,'--read-only',
            '-v',str(out/'engine-contract')+':/fixture/engine-contract:ro',
            '-e','RETIREMENT_DATABASE_URL='+dsn,'-e','RETIREMENT_WAKARU_URL=http://rollback-web:5001',
            '--entrypoint','/fixture/engine-contract',image,'-test.v','-test.run=^TestRetirementRealWakaru$')
        (out/'engine-image-contract.txt').write_bytes(result.stdout+result.stderr)
        assert b'--- PASS: TestRetirementRealWakaru' in result.stdout
        assert b'--- SKIP:' not in result.stdout and b'--- FAIL:' not in result.stdout
        report['checks'].append({'action':'engine_real_image_410','passed':True})
        stop(rollback_worker)
        probe('stage_restart','rollback-web')
        stop(rollback_web)
        assert docker('exec',redis,'redis-cli','save').stdout.strip()==b'OK'
        before=docker('exec',redis,'redis-cli','info','server').stdout
        docker('restart',redis)
        for _ in range(100):
            if docker('exec',redis,'redis-cli','ping',required=False).stdout.strip()==b'PONG':break
            time.sleep(.1)
        after=docker('exec',redis,'redis-cli','info','server').stdout
        assert next(x for x in before.splitlines() if x.startswith(b'run_id:')) != next(x for x in after.splitlines() if x.startswith(b'run_id:'))
        rollback_web=start('rollback-web',image,extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        rollback_worker=start('rollback-worker',image,['python','worker.py'],extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('after_restart','rollback-web')
        probe('restore_backup','rollback-web')
        stop(rollback_worker)
        # Stage a NEW pending direct job under rollback for the return handoff.
        probe('stage_forward','rollback-web')
        stop(rollback_web)
        forward_web=start('rollback-web',args.current_image,extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('denied','rollback-web','-returned-forward')
        result=docker('run','--rm','--pull=never','--network','container:'+pg,'--read-only',
            '-v',str(out/'engine-contract')+':/fixture/engine-contract:ro',
            '-e','RETIREMENT_DATABASE_URL='+dsn,'-e','RETIREMENT_WAKARU_URL=http://rollback-web:5001',
            '--entrypoint','/fixture/engine-contract',image,'-test.v','-test.run=^TestRetirementRealWakaru$')
        (out/'engine-forward-contract.txt').write_bytes(result.stdout+result.stderr)
        assert b'--- PASS: TestRetirementRealWakaru' in result.stdout
        assert b'--- SKIP:' not in result.stdout and b'--- FAIL:' not in result.stdout
        report['checks'].append({'action':'engine_forward_real_image_410','passed':True})
        forward_worker=start('forward-worker',args.current_image,['python','worker.py'],extra={'RECOVERY_DIRECT_MERCHANT_IDS':PILOT})
        probe('after_forward','rollback-web','-returned-forward')
        probe('after_restart','rollback-web','-returned-forward')
        report.update(forward_image=forward['forward']['Id'],forward_release_sha256=forward['release_sha256'],
            host_architecture=os.uname().machine,native_amd64=os.uname().machine in ('x86_64','amd64'))
        engine_head=(engine/'HEAD.txt').read_text().strip() if args.engine_binaries else run(['git','rev-parse','HEAD'],cwd=engine).stdout.decode().strip()
        report.update(passed=True,egress_isolated=True,dependency_packages_unchanged=build['dependencies_identical'],
            engine_source=str(engine),engine_head=engine_head,prebuilt_engine=bool(args.engine_binaries))
    finally:
        for name in reversed(containers):
            logs=docker('logs',name,required=False)
            (out/(name.removeprefix(prefix+'-')+'-final.log')).write_bytes(logs.stdout+logs.stderr)
            docker('rm','-fv',name,required=False)
        docker('volume','rm',volume,required=False);docker('network','rm',network,required=False)
        (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':main()
