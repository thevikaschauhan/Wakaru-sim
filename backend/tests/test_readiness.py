"""Issue #26 — /readiness: live, cheap checks against Zep and the LLM provider.

Mocks `app.Zep`/`app.OpenAI` so the suite never makes a real network call
(matches the rest of this suite — 205+ tests collect and run offline). Mirrors
test_api_auth.py's `/health` open-access test for the sibling `/readiness`
route.

OpenAI is used as a context manager in production code (`with OpenAI(...) as
llm_client:`), so the mocked call chain goes through
`MockOpenAI.return_value.__enter__.return_value`, not
`MockOpenAI.return_value` directly — MagicMock's `__enter__` returns a
DIFFERENT auto-generated mock unless configured, so tests that stub
`MockOpenAI.return_value.models.list` directly would silently no-op (verified
this the hard way: two tests here originally passed for the wrong reason,
reporting "ok" instead of "unreachable", until the mock chain was corrected
to go through `__enter__.return_value`).
"""
from unittest.mock import MagicMock, patch


def _llm_mock(MockOpenAI):
    """The mock actually exercised by `with OpenAI(...) as llm_client:`."""
    return MockOpenAI.return_value.__enter__.return_value


def test_readiness_ok_when_both_dependencies_reachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        _llm_mock(MockOpenAI).models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 200
    assert resp.get_json() == {"zep": "ok", "llm": "ok"}


def test_readiness_503_when_zep_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("boom")
        _llm_mock(MockOpenAI).models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "unreachable"
    assert body["llm"] == "ok"


def test_readiness_503_when_llm_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        _llm_mock(MockOpenAI).models.list.side_effect = Exception("boom")
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "ok"
    assert body["llm"] == "unreachable"


def test_readiness_503_when_both_unreachable(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("boom")
        _llm_mock(MockOpenAI).models.list.side_effect = Exception("boom")
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["zep"] == "unreachable"
    assert body["llm"] == "unreachable"


def test_readiness_does_not_leak_exception_details(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.side_effect = Exception("super-secret-connection-string")
        _llm_mock(MockOpenAI).models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert "super-secret-connection-string" not in resp.get_data(as_text=True)


def test_readiness_endpoint_open_without_key(app):
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        _llm_mock(MockOpenAI).models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 200


def test_readiness_uses_short_bounded_timeouts_not_client_defaults(app):
    """Pins the acceptance criterion itself: each check must pass its OWN short
    timeout, independent of Zep's ~60s / the LLM client's 120s read timeout
    used elsewhere. A regression that silently drops these kwargs (e.g. during
    a refactor) would otherwise pass every other test in this file."""
    with patch("app.Zep") as MockZep, patch("app.OpenAI") as MockOpenAI:
        MockZep.return_value.project.get.return_value = MagicMock()
        _llm_mock(MockOpenAI).models.list.return_value = MagicMock()
        resp = app.test_client().get("/readiness")

    assert resp.status_code == 200
    MockZep.return_value.project.get.assert_called_once_with(
        request_options={"timeout_in_seconds": 2}
    )
    _llm_mock(MockOpenAI).models.list.assert_called_once_with(timeout=2)
    # max_retries=0: a readiness probe must fail fast, not retry into the
    # 2s budget being silently multiplied.
    assert MockOpenAI.call_args.kwargs.get("max_retries") == 0
