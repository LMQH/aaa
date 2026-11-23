"""记忆宿主：aaa 拥有召回、注入、观察、预算与降级。

引擎只返回结构化条目；宿主决定何时召回、拼成哪段提示词、超时与失败如何
降级。外部引擎的召回块进入 system prompt，不写入会话历史；观察在每轮完成
后执行一次，失败只记录，不回滚会话。引擎声明的工具由宿主包装、计时并把
异常降级成 Error 文本；引擎提供的静态提示块由宿主原样追加。
"""

import inspect
import logging
import re
import threading
from contextvars import ContextVar
from typing import Any, Callable, Dict, FrozenSet, Literal, Optional, Protocol, Sequence, Tuple

from .protocol import (
    MemoryEngine,
    MemoryEngineError,
    MemoryEntry,
    MemoryIdentity,
    MemoryTool,
    Observation,
    RecallQuery,
)

logger = logging.getLogger("aaa.memory")

BLOCK_OPEN = "<recalled_memories>"
BLOCK_CLOSE = "</recalled_memories>"
BLOCK_NOTE = "以下是从记忆中召回的条目，可能过时；优先以当前对话为准。"
SEEN_LIMIT = 128
_MAIN_AGENT_ID = "aaa-main"
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Agno 会注入的回读/记忆工具名，以及 emitters 按名快捷放行的 shell 工具：
# 外置引擎不得占用，避免路由劫持或绕过确认。
RESERVED_TOOL_NAMES = frozenset({
    "read_result", "search_result", "update_user_memory", "update_session_state",
    "run_shell_command", "run_powershell",
})


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...


def _call_with_timeout(call: Callable[[], Any], timeout: float) -> Any:
    """在守护线程里执行一次引擎调用；超时或异常原样抛出。"""
    outcome: Dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # 引擎异常交由宿主统一降级
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="aaa-memory", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"超过 {timeout:g} 秒")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def render_entries(entries: Sequence[MemoryEntry], budget: int) -> str:
    """去重、按引擎顺序截断到字符预算，渲染稳定块；空结果返回空串。"""
    lines = [BLOCK_NOTE]
    used = len(BLOCK_NOTE)
    seen = set()
    for entry in entries:
        text = str(getattr(entry, "memory", "") or "").strip()
        if not text or text in seen:
            continue
        remaining = budget - used
        if remaining <= 4:
            break
        if len(text) > remaining - 4:
            text = text[: remaining - 5].rstrip() + "…"
        lines.append(f"- {text}")
        used += len(text) + 4
        seen.add(text)
    if len(lines) == 1:
        return ""
    return "\n".join([BLOCK_OPEN, *lines, BLOCK_CLOSE])


class MemoryHost:
    """应用持有的外置实例；执行体装配只借用它的回调。"""

    # 存活的待确认记忆工具名集合，键为实例 id：approve_requirements 在宿主
    # 实例未被透传时据此兜底，保证确认不失效；close 注销。
    _registered: Dict[int, FrozenSet[str]] = {}

    def __init__(self, engine: MemoryEngine, engine_id: str, settings: SettingsLike) -> None:
        self.engine = engine
        self.engine_id = engine_id
        self.settings = settings
        self._pending: Optional[Tuple[str, str]] = None
        self.continued_run_id: ContextVar[Optional[str]] = ContextVar("memory_continued_run_id", default=None)
        self._seen: set = set()
        self._specs: Tuple[MemoryTool, ...] = self._load_specs()
        type(self)._registered[id(self)] = frozenset(
            spec.name for spec in self._specs if spec.requires_confirmation
        )

    @classmethod
    def registered_confirm_names(cls) -> FrozenSet[str]:
        """当前存活宿主的全部待确认工具名。"""
        names: set = set()
        for group in cls._registered.values():
            names |= group
        return frozenset(names)

    def _load_specs(self) -> Tuple[MemoryTool, ...]:
        """读取并校验引擎声明的工具；契约不满足直接失败，不静默降级。"""
        declare = getattr(self.engine, "tools", None)
        if not callable(declare):
            return ()
        try:
            raw = list(declare() or ())
        except (Exception, SystemExit) as exc:
            raise MemoryEngineError(
                f"外置记忆引擎 {self.engine_id} 工具声明失败（{type(exc).__name__}）"
            ) from exc
        if not raw:
            return ()
        call = getattr(self.engine, "call_tool", None)
        if not callable(call):
            raise MemoryEngineError(f"外置记忆引擎 {self.engine_id} 声明了工具但缺少 call_tool")
        specs: list = []
        seen: set = set()
        for spec in raw:
            name = str(getattr(spec, "name", "") or "")
            if not TOOL_NAME.fullmatch(name):
                raise MemoryEngineError(f"外置记忆引擎 {self.engine_id} 工具名不合法：{name!r}")
            if name in seen:
                raise MemoryEngineError(f"外置记忆引擎 {self.engine_id} 工具重名：{name}")
            if name in RESERVED_TOOL_NAMES:
                raise MemoryEngineError(f"外置记忆引擎 {self.engine_id} 工具名与保留工具冲突：{name}")
            read_only = getattr(spec, "read_only", False)
            if not isinstance(read_only, bool):
                raise MemoryEngineError(
                    f"外置记忆引擎 {self.engine_id} 工具 {name} 的 read_only 必须是布尔值"
                )
            requires_confirmation = getattr(spec, "requires_confirmation", False)
            if not isinstance(requires_confirmation, bool):
                raise MemoryEngineError(
                    f"外置记忆引擎 {self.engine_id} 工具 {name} 的 requires_confirmation 必须是布尔值"
                )
            parameters = getattr(spec, "parameters", None)
            if parameters is not None and not (
                isinstance(parameters, dict) and parameters.get("type") == "object"
            ):
                raise MemoryEngineError(
                    f"外置记忆引擎 {self.engine_id} 工具 {name} 的 parameters 必须是 object schema"
                )
            seen.add(name)
            specs.append(MemoryTool(
                name=name,
                description=str(getattr(spec, "description", "") or ""),
                parameters=parameters,
                read_only=read_only,
                requires_confirmation=requires_confirmation,
            ))
        return tuple(specs)

    def close(self) -> None:
        type(self)._registered.pop(id(self), None)
        try:
            close = getattr(self.engine, "close", None)
            if callable(close):
                _call_with_timeout(close, float(self.settings.get("memory.close_timeout")))
        except (Exception, SystemExit) as exc:
            logger.warning("外置记忆关闭失败（engine=%s）：%s", self.engine_id, type(exc).__name__)

    def tool_specs(self) -> Tuple[MemoryTool, ...]:
        """引擎声明的工具；空表示引擎没有工具面。"""
        return self._specs

    def confirm_tool_names(self) -> FrozenSet[str]:
        """引擎声明需要人工确认的工具名。"""
        return frozenset(spec.name for spec in self._specs if spec.requires_confirmation)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]], identity: MemoryIdentity) -> str:
        """执行一次外置工具调用；异常、超时一律转成 Error 文本返回。"""
        call = getattr(self.engine, "call_tool", None)
        if not callable(call):
            return f"Error: {name} unavailable"
        timeout = float(self.settings.get("memory.tool_timeout"))
        try:
            result = _call_with_timeout(lambda: call(name, dict(arguments or {}), identity), timeout)
        except (Exception, SystemExit) as exc:
            logger.warning(
                "记忆工具失败（engine=%s tool=%s）：%s", self.engine_id, name, type(exc).__name__
            )
            return f"Error: {type(exc).__name__}"
        if result is None:
            return ""
        return result if isinstance(result, str) else str(result)

    def prompt_block(self) -> str:
        """引擎提供的静态提示块；缺失或失败都返回空串，不影响宿主模板。"""
        block = getattr(self.engine, "prompt_block", None)
        if not callable(block):
            return ""
        try:
            return str(block() or "").strip()
        except (Exception, SystemExit) as exc:
            logger.warning("记忆提示块失败（engine=%s）：%s", self.engine_id, type(exc).__name__)
            return ""

    def config_specs(self) -> tuple:
        """引擎声明的可调配置项（字典列表）；缺失或失败返回空，表示没有引擎级配置面。"""
        declare = getattr(self.engine, "config_specs", None)
        if not callable(declare):
            return ()
        try:
            specs = _call_with_timeout(declare, float(self.settings.get("memory.tool_timeout")))
        except (Exception, SystemExit) as exc:
            logger.warning("读取记忆引擎配置面失败（engine=%s）：%s", self.engine_id, type(exc).__name__)
            return ()
        return tuple(specs or ())

    def apply_config(self, values: Dict[str, Any]) -> str:
        """把键值交给引擎自行保存（写它自己的配置文件）；返回空表示成功。"""
        apply_ = getattr(self.engine, "apply_config", None)
        if not callable(apply_):
            return "Error: 当前引擎不支持配置修改"
        timeout = float(self.settings.get("memory.tool_timeout"))
        try:
            result = _call_with_timeout(lambda: apply_(dict(values)), timeout)
        except (Exception, SystemExit) as exc:
            logger.warning("记忆引擎配置保存失败（engine=%s）：%s", self.engine_id, type(exc).__name__)
            return f"Error: {type(exc).__name__}"
        if result is None:
            return ""
        return result if isinstance(result, str) else str(result)

    @property
    def user_id(self) -> str:
        return str(self.settings.get("memory.user_id") or "default")

    def identity(
        self, *, user_id: Optional[str] = None,
        session_id: Optional[str] = None, run_id: Optional[str] = None,
    ) -> MemoryIdentity:
        return MemoryIdentity(
            user_id=user_id or self.user_id,
            agent_id=_MAIN_AGENT_ID,
            session_id=session_id or None,
            run_id=run_id or None,
        )

    def read(
        self, *, session_id: Optional[str], limit: int, page: int,
    ) -> Optional[Tuple[Sequence[MemoryEntry], int]]:
        read = getattr(self.engine, "read", None)
        if not callable(read):
            return None
        return read(self.identity(session_id=session_id), limit=limit, page=page)

    def recall(self, text: str, *, identity: MemoryIdentity) -> str:
        """召回并渲染；异常、超时或空结果都返回空串，不阻断本轮。"""
        if not text.strip():
            return ""
        recall = self.engine.recall
        if inspect.iscoroutinefunction(recall):
            logger.warning("记忆召回失败（engine=%s）：协议只支持同步 recall", self.engine_id)
            return ""
        limit = int(self.settings.get("memory.recall_limit"))
        timeout = float(self.settings.get("memory.recall_timeout"))
        budget = int(self.settings.get("memory.recall_chars"))
        query = RecallQuery(text=text, identity=identity, limit=limit)
        try:
            entries = (_call_with_timeout(lambda: recall(query), timeout) or ())[:limit]
            block = render_entries(entries, budget)
        except (Exception, SystemExit) as exc:
            logger.warning("记忆召回失败（engine=%s）：%s", self.engine_id, type(exc).__name__)
            return ""
        logger.info(
            "记忆召回 engine=%s user=%s hits=%d chars=%d",
            self.engine_id, identity.user_id, len(entries), len(block),
        )
        return block

    def recall_hook(self, run_input, run_context, user_id: Optional[str] = None) -> None:
        """Agno pre-hook：本轮开始前召回，结果暂存给 instructions 渲染。"""
        text = run_input.input_content_string()
        identity = self.identity(
            user_id=user_id or getattr(run_context, "user_id", None),
            session_id=getattr(run_context, "session_id", None),
            run_id=getattr(run_context, "run_id", None),
        )
        block = self.recall(text, identity=identity)
        self._pending = (identity.run_id, block) if block and identity.run_id else None

    def instructions(self, base: str) -> Callable:
        """把召回块追加到 system prompt；没有命中时原样返回 base。"""

        def render(run_context) -> str:
            if run_context is None:
                return base
            pending = self._pending
            if pending and pending[0] == getattr(run_context, "run_id", ""):
                return f"{base}\n\n{pending[1]}" if base else pending[1]
            return base

        return render

    def observe_hook(self, run_output, run_context, user_id: Optional[str] = None) -> None:
        """Agno post-hook：每轮完成后提交一次观察；失败只记录。"""
        run_id = getattr(run_output, "run_id", "") or getattr(run_context, "run_id", "")
        if self._pending and self._pending[0] == run_id:
            self._pending = None
        observe = getattr(self.engine, "observe", None)
        if not callable(observe):
            return
        if inspect.iscoroutinefunction(observe):
            logger.warning("记忆观察失败（engine=%s）：协议只支持同步 observe", self.engine_id)
            return
        answer = run_output.get_content_as_string() if hasattr(run_output, "get_content_as_string") else ""
        answer = str(answer or "")
        if not answer.strip():
            return
        key = run_id or f"{getattr(run_output, 'session_id', '')}:{id(run_output)}"
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > SEEN_LIMIT:
            self._seen = set(list(self._seen)[-SEEN_LIMIT:])
        query = ""
        run_input = getattr(run_output, "input", None)
        if run_input is not None and hasattr(run_input, "input_content_string"):
            query = run_input.input_content_string()
        if getattr(run_output, "forked_from_run_id", None):
            # Agno 的续接快照保留原始 input；实际问题以本轮最后一条用户消息为准。
            for message in reversed(run_output.messages or ()):
                if message.role == "user" and not message.from_history:
                    query = message.get_content_string()
                    break
        event = Observation(
            query=query,
            answer=answer,
            identity=self.identity(
                user_id=user_id or getattr(run_context, "user_id", None),
                session_id=getattr(run_context, "session_id", None) or getattr(run_output, "session_id", None),
                run_id=run_id,
            ),
        )
        timeout = float(self.settings.get("memory.observe_timeout"))
        try:
            _call_with_timeout(lambda: observe(event), timeout)
            logger.info("记忆观察已提交 engine=%s run=%s", self.engine_id, run_id)
        except (Exception, SystemExit) as exc:
            logger.warning("记忆观察失败（engine=%s）：%s", self.engine_id, type(exc).__name__)


ActiveMemory = MemoryHost | Literal["builtin"] | None
