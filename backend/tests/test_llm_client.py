"""Tests for the LLM client response_format fallback.

Some OpenAI-compatible providers (notably DeepSeek) reject
``response_format={"type": "json_object"}`` with a 400
``BadRequestError`` ("This response_format type is unavailable now"). The
``create_chat_completion`` helper retries once without response_format so the
whole cart-recovery pipeline degrades gracefully instead of failing fast.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import BadRequestError

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
