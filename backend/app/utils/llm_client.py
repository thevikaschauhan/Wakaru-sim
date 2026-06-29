"""LLM client wrapper.

Unified OpenAI-format access to the (OpenAI-compatible) LLM provider. Every LLM
call goes through this client so the network timeout and retry policy live in
exactly one place.
"""

import json
import logging
import re
from typing import Optional, Dict, Any, List

import httpx
from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)

from ..config import Config
from .retry import retry_with_backoff

logger = logging.getLogger("mirofish.llm_client")

# Transient network errors worth retrying. BadRequestError is deliberately
# excluded: it is a client error (the response_format case is already handled by
# create_chat_completion's fallback), so retrying it would just fail again.
_RETRYABLE_LLM_EXCEPTIONS = (RateLimitError, APITimeoutError, APIConnectionError)


def create_chat_completion(client: OpenAI, **kwargs):
    """Call ``chat.completions.create``, falling back to a plain call when the
    provider rejects ``response_format``.

    Some OpenAI-compatible providers (notably DeepSeek) return
    ``400 "This response_format type is unavailable now"`` for
    ``response_format={"type": "json_object"}``. Every JSON-mode call site pairs
    response_format with prompt-level JSON instructions and downstream JSON
    recovery (chat_json strips ``` fences; the simulation generators extract the
    object via a ``{...}`` regex), so dropping response_format degrades
    gracefully instead of failing the whole cart-recovery pipeline with a
    BadRequestError. A non-response_format 400 is re-raised unchanged.
    """
    try:
        return client.chat.completions.create(**kwargs)
    except BadRequestError:
        if "response_format" not in kwargs:
            raise
        logger.warning("provider rejected response_format; retrying without it")
        kwargs.pop("response_format")
        return client.chat.completions.create(**kwargs)


class LLMClient:
    """LLM client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY is not configured")

        # Bound every call so one hung request cannot tie up a worker. The SDK's
        # built-in retries are disabled (max_retries=0) so retry is controlled
        # explicitly by the application: chat()/chat_json() via @retry_with_backoff,
        # and the simulation generators via their own JSON-repair loops (which call
        # create_chat_completion directly). max_retries=0 keeps the SDK from
        # silently compounding either.
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=5, read=120, write=30, pool=5),
            max_retries=0,
        )

    @retry_with_backoff(max_retries=3, exceptions=_RETRYABLE_LLM_EXCEPTIONS)
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """Send a chat request.

        Args:
            messages: list of chat messages
            temperature: sampling temperature
            max_tokens: maximum number of tokens to generate
            response_format: response format (e.g. JSON mode)

        Returns:
            The model's response text.

        Retries transient network errors (rate limit / timeout / connection) with
        exponential backoff. chat_json() routes through this method, so the retry
        is applied here only — decorating both would nest the retry loops.
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if response_format:
            kwargs["response_format"] = response_format

        response = create_chat_completion(self.client, **kwargs)
        content = response.choices[0].message.content
        # Some models (e.g. MiniMax M2.5) wrap reasoning in <think> tags; strip it.
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """Send a chat request and return parsed JSON.

        Args:
            messages: list of chat messages
            temperature: sampling temperature
            max_tokens: maximum number of tokens to generate

        Returns:
            The parsed JSON object.
        """
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        # Strip markdown code-fence markers.
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"LLM returned invalid JSON: {cleaned_response}")
