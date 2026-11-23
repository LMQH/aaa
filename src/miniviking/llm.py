"""共享模型客户端：默认继承 aaa 的默认渠道，也可由引擎配置覆盖。

只使用 OpenAI 兼容的 chat completions；aaa 的两种协议（deepseek /
openai-completions）都走同一端点形态。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from .config import LLMSettings

logger = logging.getLogger("aaa.memory.miniviking")


class ChatClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    api_key: str
    model: str
    provider: str = ""
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout: float = 90.0


def openai_client(config: ChatConfig):
    from openai import OpenAI

    kwargs = dict(base_url=config.base_url, api_key=config.api_key, timeout=config.timeout)
    try:
        return OpenAI(**kwargs)
    except ImportError:
        # 只有 SOCKS 代理、但环境里没有 socksio 时，httpx 会在构造期失败；
        # 显式关掉环境代理，只保留可用的 HTTP(S) 代理。
        import httpx2

        http_client = httpx2.Client(proxy=_http_proxy(), trust_env=False)
        logger.warning("检测到 SOCKS 代理但缺少 socksio，已改用 HTTP(S) 代理或直连")
        return OpenAI(**kwargs, http_client=http_client)


class OpenAIChat:
    def __init__(self, config: ChatConfig) -> None:
        self._config = config
        self._client = openai_client(config)

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._config.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""


def _http_proxy() -> Optional[str]:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value
    return None


def build_client(settings: LLMSettings) -> Optional[ChatClient]:
    config = resolve(settings)
    if config is None:
        return None
    try:
        return OpenAIChat(config)
    except Exception as exc:
        logger.warning("miniviking 模型客户端初始化失败：%s", type(exc).__name__)
        return None


def resolve(settings: LLMSettings) -> Optional[ChatConfig]:
    """显式配置优先；否则继承 aaa 的默认渠道与模型。"""
    explicit_key = settings.api_key or os.environ.get(settings.api_key_env, "")
    if settings.base_url and settings.model and explicit_key:
        return ChatConfig(
            base_url=settings.base_url,
            api_key=explicit_key,
            model=settings.model,
            provider=settings.provider,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            timeout=settings.timeout,
        )
    if not settings.inherit:
        return None
    try:
        from aaa.config.loader import load_config
        from aaa.config.settings import Settings

        config = load_config(Settings())
    except Exception as exc:
        logger.warning("miniviking 无法读取 aaa 默认模型：%s", type(exc).__name__)
        return None
    if not config:
        return None
    try:
        provider = str(config["default"]["provider"])
        model = str(config["default"]["model"])
        channel = config["providers"][provider]
        base_url = str(channel["base_url"])
        api_key = str(channel["api_key"])
    except (KeyError, TypeError):
        return None
    if not (base_url and api_key and model):
        return None
    return ChatConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider=provider,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict:
    """从模型输出里取出第一个 JSON 对象；解析失败抛 ValueError。"""
    cleaned = _FENCE.sub("", str(text or "").strip()).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出没有 JSON 对象")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型输出的 JSON 不是对象")
    return value
