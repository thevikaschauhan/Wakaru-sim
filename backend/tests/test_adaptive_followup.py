"""The real app factory mounts AF06 dark, behind the existing API guard."""

from cart_recovery.adaptive.http import PATH


def test_adaptive_endpoint_registered_and_disabled(app, monkeypatch):
    monkeypatch.delenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED", raising=False)
    assert app.test_client().post(PATH, json={}).status_code == 401
    response = app.test_client().post(
        PATH, json={}, headers={"X-API-Key": "test-wakaru-api-key"}
    )
    assert (
        response.status_code == 503
        and response.json["error"] == "adaptive_planning_disabled"
    )


def test_adaptive_endpoint_requires_its_own_bound_signature(app, monkeypatch):
    monkeypatch.setenv("ADAPTIVE_FOLLOWUP_PLANNING_ENABLED", "true")
    response = app.test_client().post(
        PATH, json={}, headers={"X-API-Key": "test-wakaru-api-key"}
    )
    assert response.status_code == 401
