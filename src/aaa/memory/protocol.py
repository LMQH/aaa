"""外置记忆引擎的最小协议：结构化召回、观察与可选工具。

引擎只返回结构化命中，不拼提示词、不碰 Agent 执行路径。核心方法只有
`recall`；`observe`、`read`、`tools` + `call_tool` 与 `prompt_block` 都是
可选能力，缺少时由宿主跳过或明确报不支持。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Sequence


@dataclass(frozen=True)
class MemoryEntry:
    """一条记忆；召回与分页读取共用同一形状，不依赖引擎内部格式。"""

    memory: str
    memory_id: str = ""
    topics: Sequence[str] = ()
    updated_at: Optional[int] = None
    score: Optional[float] = None


@dataclass(frozen=True)
class MemoryIdentity:
    """aaa 提供的身份；没有当前会话或运行时，相应字段为 None。"""

    user_id: str
    agent_id: str
    session_id: Optional[str] = None
    run_id: Optional[str] = None


@dataclass(frozen=True)
class RecallQuery:
    """一次召回的作用域与预算；所有字段由宿主填入，引擎不自行推断。"""

    text: str
    identity: MemoryIdentity
    limit: int = 5


@dataclass(frozen=True)
class Observation:
    """一轮完成后的观察事件；`identity.run_id` 供引擎做幂等。"""

    query: str
    answer: str
    identity: MemoryIdentity


class MemoryEngine(Protocol):
    """外部记忆引擎必须实现的最小接口；实现方不得接管 Agent 执行路径。"""

    def recall(self, query: RecallQuery) -> Sequence[MemoryEntry]: ...


class MemoryEngineError(ValueError):
    """外置引擎来源、发现、构造或契约不满足；调用方应在启动期暴露。"""


@dataclass(frozen=True)
class MemoryTool:
    """一个外置记忆工具的声明；schema 由引擎给出，aaa 只做装配、确认与分发。

    `parameters` 是 JSON Schema 对象（缺省为无参数）。`read_only` 描述工具
    是否只读；只有 `requires_confirmation=True` 才请求宿主确认。
    """

    name: str
    description: str = ""
    parameters: Optional[Dict[str, Any]] = None
    read_only: bool = False
    requires_confirmation: bool = False
