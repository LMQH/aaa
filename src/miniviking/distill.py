"""LLM 抽取与摘要：把一轮对话蒸馏成记忆节点，把资源片段摘要成可检索文本。

模型只负责理解与归纳；写入、去重、索引由引擎负责。模型不可用时退化为
机械摘要，保证记忆仍然可写、可查。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .llm import ChatClient, parse_json

TURN_SYSTEM = """你是 MiniViking 的记忆整理器。输入是一轮对话和已有会话摘要。
只提取对后续对话有用的持久信息：用户偏好、稳定事实、决定、约定、待办、关键结论。
忽略寒暄、临时推理、重复内容和纯工具输出。

输出严格 JSON，不要输出任何其他文字：
{"memories":[{"title":"简短标题，不超过30字","abstract":"一句话摘要，不超过120字","overview":"更完整的概述，不超过400字","body":"自包含的正文，说明背景、结论与适用条件","tags":["标签"],"keywords":["检索关键词，含同义词与专有名词"],"importance":0.0}],"session":{"summary":"滚动更新后的会话摘要，不超过800字","topics":["主题"],"keywords":["关键词"]}}

规则：
- memories 最多 %d 条；没有值得长期保留的信息时输出空数组。
- 同一事实不要拆成多条；标题要能独立识别。
- importance 取 0 到 1，越稳定、越常用越高。
- 会话摘要要覆盖此前的累计内容，而不是只写本轮。"""

CHUNK_SYSTEM = """你是 MiniViking 的文档摘要器。为给定文本片段生成检索用摘要。
只输出严格 JSON：
{"abstract":"一句话摘要，不超过120字","overview":"更完整概述，不超过400字","keywords":["检索关键词"],"tags":["分类标签"]}
不要输出 JSON 以外的文字。"""


def distill_turn(
    client: ChatClient, *, query: str, answer: str, previous_summary: str,
    limit: int, max_chars: int,
) -> dict:
    payload = _clip_turn(query, answer, max_chars)
    user = (
        f"已有会话摘要：\n{previous_summary or '（无）'}\n\n"
        f"本轮用户输入：\n{payload[0]}\n\n"
        f"本轮助手回答：\n{payload[1]}"
    )
    raw = client.complete(TURN_SYSTEM % max(limit, 1), user)
    return _normalize(parse_json(raw), limit)


def summarize_text(client: ChatClient, text: str, max_chars: int) -> dict:
    raw = client.complete(CHUNK_SYSTEM, _clip(text, max_chars))
    data = parse_json(raw)
    return {
        "abstract": _text(data.get("abstract"), 240),
        "overview": _text(data.get("overview"), 1200),
        "keywords": _list(data.get("keywords")),
        "tags": _list(data.get("tags")),
    }


def mechanical_turn(query: str, answer: str, limit: int) -> dict:
    """模型不可用时的退化：把本轮保留成一条可检索记录。"""
    query = str(query or "").strip()
    answer = str(answer or "").strip()
    if not query and not answer:
        return {"memories": [], "session": {"summary": "", "topics": [], "keywords": []}}
    title = _first_line(query) or _first_line(answer) or "对话记录"
    body = f"用户：{query}\n\n助手：{answer}".strip()
    memories = [{
        "title": title[:60],
        "abstract": _clip(answer or query, 240),
        "overview": _clip(answer or query, 1200),
        "body": body,
        "tags": ["对话"],
        "keywords": _keywords(query + " " + answer),
        "importance": 0.3,
    }][: max(limit, 1)]
    summary = _clip(f"用户：{query}\n助手：{answer}", 800)
    return {
        "memories": memories,
        "session": {"summary": summary, "topics": ["对话"], "keywords": _keywords(query + " " + answer)},
    }


def mechanical_summary(text: str) -> dict:
    value = str(text or "").strip()
    heading = _first_line(value)
    return {
        "abstract": _clip(heading or value, 240),
        "overview": _clip(value, 1200),
        "keywords": _keywords(value),
        "tags": [],
    }


def _clip_turn(query: str, answer: str, max_chars: int) -> tuple[str, str]:
    budget = max(max_chars, 500)
    query = _clip(query, budget // 3)
    answer = _clip(answer, budget - len(query))
    return query, answer


def _clip(text: object, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: max(limit - 1, 1)].rstrip() + "…"


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned
    return ""


def _text(value: object, limit: int) -> str:
    return _clip(value, limit)


def _list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text[:40])
    return result[:16]


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", str(text or ""))
    result = []
    for word in words:
        if word not in result:
            result.append(word)
    return result[:16]


def _normalize(data: dict, limit: int) -> dict:
    memories = []
    raw_items = data.get("memories")
    if isinstance(raw_items, list):
        for item in raw_items[: max(limit, 1)]:
            if not isinstance(item, dict):
                continue
            title = _text(item.get("title"), 80).strip()
            body = _text(item.get("body"), 8000).strip()
            if not title and not body:
                continue
            abstract = _text(item.get("abstract"), 240)
            overview = _text(item.get("overview"), 1200)
            memories.append({
                "title": title or abstract[:60] or "记忆",
                "abstract": abstract or overview[:240] or body[:240],
                "overview": overview or abstract or body[:1200],
                "body": body or overview or abstract,
                "tags": _list(item.get("tags")),
                "keywords": _list(item.get("keywords")) or _keywords(title + " " + body),
                "importance": _importance(item.get("importance")),
            })
    session_raw = data.get("session")
    session = {"summary": "", "topics": [], "keywords": []}
    if isinstance(session_raw, dict):
        session = {
            "summary": _text(session_raw.get("summary"), 2000),
            "topics": _list(session_raw.get("topics")),
            "keywords": _list(session_raw.get("keywords")),
        }
    return {"memories": memories, "session": session}


def _importance(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(max(number, 0.0), 1.0)


def dedupe_key(title: str, body: str) -> str:
    return hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()[:12]
