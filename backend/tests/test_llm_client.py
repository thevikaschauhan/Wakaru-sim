"""Tests for the LLM client response_format fallback.

Some OpenAI-compatible providers (notably DeepSeek) reject
``response_format={"type": "json_object"}`` with a 400
``BadRequestError`` ("This response_format type is unavailable now"). The
``create_chat_completion`` helper retries once without response_format so the
whole cart-recovery pipeline degrades gracefully instead of failing fast.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)

from app.utils.llm_client import LLMClient, create_chat_completion


def _bad_request(msg="This response_format type is unavailable now"):
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    return BadRequestError(msg, response=httpx.Response(400, request=req), body=None)


def _completion(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")]
    )


def test_helper_drops_response_format_on_bad_request():
    client = MagicMock()
    client.chat.completions.create.side_effect = [_bad_request(), _completion('{"ok": true}')]

    out = create_chat_completion(
        client,
        model="m",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )

    assert out.choices[0].message.content == '{"ok": true}'
    assert client.chat.completions.create.call_count == 2
    # first call carried response_format; the retry dropped it
    assert "response_format" in client.chat.completions.create.call_args_list[0].kwargs
    assert "response_format" not in client.chat.completions.create.call_args_list[1].kwargs


def test_helper_reraises_non_response_format_bad_request():
    client = MagicMock()
    client.chat.completions.create.side_effect = _bad_request("invalid messages")

    with pytest.raises(BadRequestError):
        create_chat_completion(client, model="m", messages=[{"role": "user", "content": "x"}])

    # No response_format was sent → nothing to fall back to → no retry.
    assert client.chat.completions.create.call_count == 1


def test_helper_reraises_when_retry_also_fails():
    client = MagicMock()
    client.chat.completions.create.side_effect = [_bad_request(), _bad_request("still bad")]

    with pytest.raises(BadRequestError):
        create_chat_completion(
            client,
            model="m",
            messages=[{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
        )

    assert client.chat.completions.create.call_count == 2  # one retry, then re-raise


def test_helper_passthrough_when_no_error():
    client = MagicMock()
    client.chat.completions.create.return_value = _completion("hi")

    out = create_chat_completion(
        client,
        model="m",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )

    assert out.choices[0].message.content == "hi"
    assert client.chat.completions.create.call_count == 1


def test_chat_json_recovers_after_response_format_dropped():
    # End-to-end through LLMClient.chat_json: the provider rejects response_format,
    # the fallback returns fenced JSON, and chat_json strips the fence and parses it.
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = [
        _bad_request(),
        _completion('```json\n{"reason": "shipping"}\n```'),
    ]

    result = llm.chat_json([{"role": "user", "content": "return json"}])

    assert result == {"reason": "shipping"}
    assert llm.client.chat.completions.create.call_count == 2


# --- #22: client timeout, centralized retry/backoff, DI wiring -----------------

def _request():
    return httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _rate_limit(msg="rate limited"):
    return RateLimitError(msg, response=httpx.Response(429, request=_request()), body=None)


def _timeout():
    return APITimeoutError(request=_request())


def _connection():
    return APIConnectionError(message="connection reset", request=_request())


def test_llmclient_configures_timeout_and_disables_sdk_retries():
    # AC#2 (unit form): the shared client is built with a bounded timeout and the
    # SDK's built-in retries disabled (retry is owned once at the chat layer), so
    # a hung call fails in ~120s rather than hanging the worker indefinitely.
    with patch("app.utils.llm_client.OpenAI") as mock_openai:
        LLMClient(api_key="test")

    assert mock_openai.call_count == 1
    kwargs = mock_openai.call_args.kwargs
    assert kwargs["max_retries"] == 0
    timeout = kwargs["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5, 120, 30, 5)


def test_chat_retries_on_rate_limit_then_succeeds():
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = [
        _rate_limit(),
        _rate_limit(),
        _completion("ok"),
    ]

    with patch("app.utils.retry.time.sleep"):
        out = llm.chat([{"role": "user", "content": "hi"}])

    assert out == "ok"
    assert llm.client.chat.completions.create.call_count == 3


def test_chat_retries_on_timeout_then_succeeds():
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = [_timeout(), _completion("done")]

    with patch("app.utils.retry.time.sleep"):
        out = llm.chat([{"role": "user", "content": "hi"}])

    assert out == "done"
    assert llm.client.chat.completions.create.call_count == 2


def test_chat_retries_on_connection_error_then_succeeds():
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = [_connection(), _completion("ok")]

    with patch("app.utils.retry.time.sleep"):
        out = llm.chat([{"role": "user", "content": "hi"}])

    assert out == "ok"
    assert llm.client.chat.completions.create.call_count == 2


def test_chat_raises_after_retries_exhausted():
    # max_retries=3 → 1 initial attempt + 3 retries = 4 calls, then re-raise.
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = _rate_limit()

    with patch("app.utils.retry.time.sleep"):
        with pytest.raises(RateLimitError):
            llm.chat([{"role": "user", "content": "hi"}])

    assert llm.client.chat.completions.create.call_count == 4


def test_chat_does_not_retry_bad_request():
    # A non-response_format BadRequestError is a client error, not transient.
    # create_chat_completion re-raises it and it is not in the retry set.
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.side_effect = _bad_request("invalid messages")

    with patch("app.utils.retry.time.sleep"):
        with pytest.raises(BadRequestError):
            llm.chat([{"role": "user", "content": "hi"}])

    assert llm.client.chat.completions.create.call_count == 1


def test_chat_json_does_not_retry_on_bad_json():
    # Malformed JSON raises ValueError in chat_json (after a successful network
    # call). ValueError is not a retryable network error, so there is no retry.
    llm = LLMClient(api_key="test")
    llm.client = MagicMock()
    llm.client.chat.completions.create.return_value = _completion("not json at all")

    with patch("app.utils.retry.time.sleep"):
        with pytest.raises(ValueError):
            llm.chat_json([{"role": "user", "content": "x"}])

    assert llm.client.chat.completions.create.call_count == 1


def test_config_generator_routes_through_injected_client():
    # Fork A2: the generator takes an injected LLMClient and issues its LLM call
    # through that client (no bare OpenAI of its own).
    from app.services.simulation_config_generator import SimulationConfigGenerator

    fake_llm = SimpleNamespace(client=MagicMock())
    fake_llm.client.chat.completions.create.return_value = _completion('{"ok": true}')

    gen = SimulationConfigGenerator(llm_client=fake_llm)
    out = gen._call_llm_with_retry("prompt", "system")

    assert gen.llm is fake_llm
    assert out == {"ok": True}
    assert fake_llm.client.chat.completions.create.called


def test_oasis_generator_accepts_injected_client():
    # Fork A2: the generator stores the injected LLMClient instead of building
    # its own OpenAI client.
    from app.services.oasis_profile_generator import OasisProfileGenerator

    fake_llm = SimpleNamespace(client=MagicMock())
    gen = OasisProfileGenerator(llm_client=fake_llm)

    assert gen.llm is fake_llm
