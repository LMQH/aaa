"""离线测试夹具：按脚本回放的假模型，遵循 Agno `Model` 的应答契约。

用途：在不联网、不花钱的前提下测 Agent 循环（工具往返、HITL 暂停与续跑）。
假模型只实现 `invoke` 及其流式/异步变体；消息拼装、工具执行、会话写入
仍由 Agno 自己完成——这样测到的就是真实循环，而不是另写一套。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from agno.models.base import Model
from agno.models.response import ModelResponse


def text(content: str) -> ModelResponse:
    """一条纯文本回复。"""
    return ModelResponse(content=content)


def tool_call(name: str, args: Dict[str, Any] | None = None, call_id: str | None = None) -> ModelResponse:
    """一条工具调用回复（OpenAI 风格：arguments 是 JSON 字符串）。"""
    return ModelResponse(
        content=None,
        tool_calls=[
            {
                "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {}, ensure_ascii=False),
                },
            }
        ],
    )


class FakeModel(Model):
    """按脚本顺序回放的模型；脚本耗尽即失败，避免静默吞掉多余调用。"""

    def __init__(self, script: List[ModelResponse]) -> None:
        self.id = "fake-model"
        self.name = "Fake"
        self.provider = "Fake"
        self.script = list(script)
        self.invocations: List[Dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> ModelResponse:
        self.invocations.append(kwargs)
        if not self.script:
            raise AssertionError("FakeModel 脚本已耗尽：实际调用次数多于预期")
        return self.script.pop(0)

    async def ainvoke(self, **kwargs: Any) -> ModelResponse:
        return self.invoke(**kwargs)

    def invoke_stream(self, **kwargs: Any):
        yield self.invoke(**kwargs)

    async def ainvoke_stream(self, **kwargs: Any):
        yield self.invoke(**kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response
