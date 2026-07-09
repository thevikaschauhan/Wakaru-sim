"""Issue #26 — /readiness: live, cheap checks against Zep and the LLM provider.

Mocks `app.Zep`/`app.OpenAI` so the suite never makes a real network call
(matches the rest of this suite — 205+ tests collect and run offline). Mirrors
test_api_auth.py's `/health` open-access test for the sibling `/readiness`
route.
"""
from unittest.mock import MagicMock, patch


def test_readiness_ok_when_both_dependencies_reachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        MockOpenAI.return_value.models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 200
    assert resp.get_json() == {"zep": "ok", "llm": "ok"}


def test_readiness_503_when_zep_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("boom")
        MockOpenAI.return_value.models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "unreachable"
    assert body["llm"] == "ok"


def test_readiness_503_when_llm_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        MockOpenAI.return_value.models.list.side_effect = Exception("boom")
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "ok"
    assert body["llm"] == "unreachable"


def test_readiness_503_when_both_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("boom")
        MockOpenAI.return_value.models.list.side_effect = Exception("boom")
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "unreachable"
    assert body["llm"] == "unreachable"


def test_readiness_does_not_leak_exception_details(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("super-secret-connection-string")
        MockOpenAI.return_value.models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert "super-secret-connection-string" not in resp.get_data(as_text=True)


def test_readiness_endpoint_open_without_key(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        MockOpenAI.return_value.models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 200
