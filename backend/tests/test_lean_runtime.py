"""Behavioral checks for the direct runtime without simulation providers."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest


def test_direct_readiness_does_not_need_simulation_providers(monkeypatch):
    monkeypatch.setenv('RECOVERY_ANALYSIS_MODE', 'legacy_simulation')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('ZEP_API_KEY', raising=False)
    from app import create_app
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    # A pure observation request has no provider dependency.
    with patch('socket.socket.connect', side_effect=AssertionError('unexpected network')):
        response = app.test_client().get('/readiness')
    assert response.status_code == 200, response.json
    assert response.json == {'analysis': 'ok'}


@pytest.mark.parametrize('packaged', [False, True])
def test_signed_job_runs_without_optional_packages(packaged, tmp_path):
    root = Path(__file__).resolve().parents[2]
    if packaged:
        artifact = tmp_path / 'runtime'
        subprocess.run([sys.executable, str(root/'ops/runtime/build.py'), str(root), str(artifact)], check=True)
        root = artifact
        assert not (root/'backend/app/services/legacy_cart_recovery.py').exists()
        assert not list(root.rglob('test_*.py'))
        assert not (root/'backend/scripts').exists()
    code = r'''
import hashlib, hmac, importlib.abc, json, sys, time
class NoLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'zep_cloud', 'openai', 'torch', 'camel', 'oasis'} or fullname.startswith(('app.services.simulation', 'app.services.report_agent', 'app.services.legacy_cart_recovery')):
            raise ImportError('optional module unavailable: ' + fullname)
sys.meta_path.insert(0, NoLegacy())
import run, worker
from rq import Queue
import fakeredis
import app.api.cart_recovery as api
connection = fakeredis.FakeStrictRedis()
queue = Queue('cart-analysis', connection=connection, is_async=False)
api.get_analyze_queue = lambda connection=None: queue
api.get_redis_connection = lambda: connection
app = run.app
app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
client = app.test_client()
body = json.dumps({'customer_id':'fixture', 'email':'fixture@example.com', 'event_id':'episode-1',
    'evidence_events':[{'event_id':'e1','event_name':'alert_displayed','occurred_at':'2026-09-15T03:58:41.885Z','alert_type':'PAYMENT_ERROR'}]})
ts = str(int(time.time()))
sig = hmac.new(b'fixture-internal', (ts+'.'+body).encode(), hashlib.sha256).hexdigest()
headers = {'X-Merchant-Id':'11111111-1111-4111-8111-111111111111', 'X-API-Key':'fixture-api', 'X-Wakaru-Timestamp':ts, 'X-Wakaru-Signature':sig, 'Content-Type':'application/json'}
response = client.post('/api/cart-recovery/jobs', data=body, headers=headers)
assert response.status_code == 202, response.json
result = client.get(response.json['status_url'], headers={'X-API-Key':'fixture-api','X-Merchant-Id':'11111111-1111-4111-8111-111111111111'})
assert result.status_code == 200, result.json
assert result.json['result']['buyer_state']['observed_friction'] == [{'code':'PAYMENT_ERROR','evidence_ids':['e1']}], result.json
assert client.get('/readiness').status_code == 200
for path in ['/api/internal/v2/recovery/evaluate', '/api/internal/v2/recovery/check-draft']:
    disabled = client.post(path, json={}, headers={'X-API-Key':'fixture-api','X-Merchant-Id':'11111111-1111-4111-8111-111111111111'})
    assert disabled.status_code == 503, (path, disabled.status_code, disabled.json)
assert client.post('/internal/integrations/analyze', json={}).status_code == 404
# A packaged image refuses global direct admission as well.
from pathlib import Path
if not Path('backend/app/services/legacy_cart_recovery.py').exists():
    import os
    from app import create_app
    sys.meta_path.pop(0)  # Allow find_spec to observe the physically absent adapter.
    os.environ['RECOVERY_ANALYSIS_MODE'] = 'direct_v1'
    try:
        create_app()
    except RuntimeError as exc:
        assert 'retirement requires legacy_simulation denial default' in str(exc)
    else:
        raise AssertionError('lean image accepted global direct mode')
assert not any(n.startswith(('app.services.simulation','app.services.report_agent','app.services.legacy_cart_recovery')) for n in sys.modules)
print('lean signed queue verified')
'''
    env = {**os.environ, 'RECOVERY_ANALYSIS_MODE':'legacy_simulation', 'RECOVERY_DIRECT_MERCHANT_IDS':'11111111-1111-4111-8111-111111111111', 'TYPESAFE_RECOVERY_MODE':'off',
           'SECRET_KEY':'fixture-secret', 'WAKARU_API_KEY':'fixture-api',
           'WAKARU_INTERNAL_SECRET':'fixture-internal', 'LLM_API_KEY':'', 'ZEP_API_KEY':'',
           'SENTRY_DSN':'', 'REDIS_URL':'', 'PYTHONDONTWRITEBYTECODE':'1',
           'PYTHONPATH':os.pathsep.join([str(root/'backend'), str(root), os.environ.get('PYTHONPATH','')])}
    result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'lean signed queue verified' in result.stdout


def test_enabled_intelligence_readiness_checks_its_own_configuration(monkeypatch):
    monkeypatch.setenv('RECOVERY_ANALYSIS_MODE', 'legacy_simulation')
    monkeypatch.setenv('TYPESAFE_RECOVERY_MODE', 'assist')
    monkeypatch.setenv('TYPESAFE_RECOVERY_MERCHANTS', 'fixture-merchant')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    from app import create_app
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    response = app.test_client().get('/readiness')
    assert response.status_code == 503
    assert response.json == {'analysis':'ok', 'intelligence_configuration':'unavailable'}
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-key')
    monkeypatch.setenv('TYPESAFE_APPROVED_MODELS', 'typesafe/jev-1.13')
    monkeypatch.setenv('TYPESAFE_JOURNAL_PATH', '/tmp/fixture-journal.sqlite')
    response = app.test_client().get('/readiness')
    assert response.status_code == 200
    assert response.json == {'analysis':'ok', 'intelligence_configuration':'ok'}


@pytest.mark.parametrize('captured_mode', [True, False])
def test_old_queued_job_is_not_reinterpreted_as_direct(monkeypatch, captured_mode):
    # Missing/explicit legacy remains legacy after a rollout, and is refused.
    from dataclasses import asdict
    from app.services.cart_recovery_jobs import AnalysisJobError, run_analysis_job
    from cart_recovery.shopify_formatter import ShopifyCartData
    from rq import Queue
    from rq.job import Job
    import fakeredis

    cart = ShopifyCartData(customer_id='fixture', customer_name='', email='fixture@example.com', cart_items=[], cart_total=0, analysis_mode='legacy_simulation')
    payload = asdict(cart)
    if not captured_mode:
        payload.pop('analysis_mode')
    queue = Queue('compatibility', connection=fakeredis.FakeStrictRedis(), is_async=True)
    job = queue.enqueue(run_analysis_job, payload)
    monkeypatch.setenv('RECOVERY_ANALYSIS_MODE', 'direct_v1')
    restored = Job.fetch(job.id, connection=queue.connection)
    with pytest.raises(AnalysisJobError, match='LegacySimulationRetired'):
        restored.func(*restored.args, **restored.kwargs)
