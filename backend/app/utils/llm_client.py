"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import logging
import re
from typing import Optional, Dict, Any, List
from openai import OpenAI, BadRequestError

from ..config import Config

logger = logging.getLogger("mirofish.llm_client")


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
    """LLM客户端"""
    
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
            raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
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
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        # 清理markdown代码块标记
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"LLM返回的JSON格式无效: {cleaned_response}")

