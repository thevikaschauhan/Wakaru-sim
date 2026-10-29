"""Readiness validates the admission policy without contacting retired providers."""
from unittest.mock import patch
import pytest
from app import create_app


def test_readiness_does_not_contact_simulation_providers(app):
    with patch('socket.socket.connect', side_effect=AssertionError('unexpected provider access')):
        response = app.test_client().get('/readiness')
    assert response.status_code == 200
    assert response.json == {'analysis': 'ok'}


def test_empty_allowlist_is_healthy_deny_default(monkeypatch):
    monkeypatch.setenv('RECOVERY_DIRECT_MERCHANT_IDS', '')
    monkeypatch.delenv('RECOVERY_ANALYSIS_MODE')
    monkeypatch.delenv('LLM_API_KEY'); monkeypatch.delenv('ZEP_API_KEY')
    response = create_app().test_client().get('/readiness')
    assert response.status_code == 200
    assert response.json == {'analysis': 'ok'}


@pytest.mark.parametrize('setting,value', [
    ('RECOVERY_ANALYSIS_MODE', 'direct_v1'),
    ('RECOVERY_ANALYSIS_MODE', 'future'),
    ('RECOVERY_DIRECT_MERCHANT_IDS', '*'),
    ('RECOVERY_DIRECT_MERCHANT_IDS', '00000000-0000-0000-0000-000000000000'),
    ('RECOVERY_DIRECT_MERCHANT_IDS', 'private-invalid-value'),
])
def test_readiness_rejects_configuration_drift_without_leaking_values(app, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    response = app.test_client().get('/readiness')
    assert response.status_code == 503
    assert response.json == {'analysis': 'unavailable'}
    assert value not in response.get_data(as_text=True)


def test_readiness_endpoint_open_without_key(app):
    response = app.test_client().get('/readiness')
    assert response.status_code == 200
