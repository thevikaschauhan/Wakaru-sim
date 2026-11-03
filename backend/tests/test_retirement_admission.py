"""Authenticated submission policy is checked before queue or provider access."""
from unittest.mock import patch
import pytest

PILOT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
PAYLOAD = {'customer_id': 'fixture', 'email': 'fixture@example.test', 'merchant_id': PILOT}


@pytest.mark.parametrize('route', ['/api/cart-recovery/jobs', '/api/cart-recovery/analyze'])
@pytest.mark.parametrize('merchant', [PILOT, OTHER, None])
def test_empty_allowlist_denies_every_authenticated_merchant(client, monkeypatch, route, merchant):
    monkeypatch.setenv('RECOVERY_DIRECT_MERCHANT_IDS', '')
    client.environ_base.pop('HTTP_X_MERCHANT_ID')
    headers = {'X-Merchant-Id': merchant} if merchant else {}
    with patch('socket.socket.connect', side_effect=AssertionError('admission caused network access')):
        response = client.post(route, json=PAYLOAD, headers=headers)
    assert response.status_code == 410
    assert response.json == {'success': False, 'error': 'legacy_simulation_retired'}


@pytest.mark.parametrize('route', ['/api/cart-recovery/jobs', '/api/cart-recovery/analyze'])
@pytest.mark.parametrize('setting,value', [('RECOVERY_ANALYSIS_MODE','direct_v1'),('RECOVERY_DIRECT_MERCHANT_IDS','*')])
def test_invalid_runtime_configuration_denies_before_network(client, monkeypatch, route, setting, value):
    monkeypatch.setenv(setting, value)
    with patch('socket.socket.connect', side_effect=AssertionError('admission caused network access')):
        response = client.post(route, json=PAYLOAD)
    assert response.status_code == 503
    assert response.json == {'success': False, 'error': 'Analysis mode unavailable'}
