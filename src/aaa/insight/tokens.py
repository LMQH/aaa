"""上下文估算：字节预筛 + Agno 计数链 + 内容哈希缓存。只读，不做装配。"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Optional, Sequence

from agno.tools.function import Function
from agno.utils import tokens as agno_tokens

_PREFILTER_BYTES = 256 * 1024
_CACHE_MAX_ENTRIES = 256
_CACHE_MAX_CHARS = 16 * 1024


class CountResult:
    """一个计数结果；approximate=True 表示走了字节估算而非分词器。"""

    __slots__ = ("tokens", "approximate")

    def __init__(self, tokens: int, approximate: bool) -> None:
        self.tokens = tokens
        self.approximate = approximate


_cache: "OrderedDict[bytes, CountResult]" = OrderedDict()


def tokenizer_label(model_id: str) -> str:
    """Agno 选择链对应的可读名：HF tokenizer / tiktoken(name) / 字符启发式。"""
    try:
        kind, tokenizer = agno_tokens._select_tokenizer(model_id)
    except Exception:
        return "字符启发式"
    if kind == "huggingface":
        return "HF tokenizer"
    if kind == "tiktoken":
        return f"tiktoken {getattr(tokenizer, 'name', '')}".strip()
    return "字符启发式"


def count_text(text: str, model_id: str) -> CountResult:
    """估算一段文本：超大字串走字节/4，其余走 Agno 计数（失败同降级），带缓存。"""
    if not text:
        return CountResult(0, False)
    data = text.encode("utf-8")
    if len(data) > _PREFILTER_BYTES:
        return CountResult((len(data) + 3) // 4, True)
    key = hashlib.blake2b(data, digest_size=16).digest()
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached
    try:
        result = CountResult(agno_tokens.count_text_tokens(text, model_id), False)
    except Exception:
        result = CountResult((len(data) + 3) // 4, True)
    if len(text) <= _CACHE_MAX_CHARS:
        _cache[key] = result
        if len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return result


def _tool_payload(tool: Any) -> dict:
    if isinstance(tool, Function):
        return tool.to_dict()
    if isinstance(tool, dict):
        return tool
    return {
        "name": getattr(tool, "name", "?"),
        "description": getattr(tool, "description", ""),
        "parameters": getattr(tool, "parameters", None),
    }


def tool_schema_payloads(tools: Sequence[Any]) -> list[dict]:
    """把 Agent 上的工具对象展开成 Agno 计数函数接受的字典列表。"""
    payloads: list[dict] = []
    for tool in tools or ():
        functions = getattr(tool, "functions", None)
        if isinstance(functions, dict):
            for fn in functions.values():
                payloads.append(_tool_payload(fn))
            continue
        payloads.append(_tool_payload(tool))
    return payloads


def count_tool_schemas(tools: Sequence[Any], model_id: str) -> CountResult:
    """工具 schema 总量：走 Agno 现成的工具计数，失败降级为字节估算。"""
    payloads = tool_schema_payloads(tools)
    if not payloads:
        return CountResult(0, False)
    try:
        return CountResult(agno_tokens.count_tool_tokens(payloads, model_id), False)
    except Exception:
        text = json.dumps(payloads, ensure_ascii=False, default=str)
        return CountResult((len(text.encode("utf-8")) + 3) // 4, True)
