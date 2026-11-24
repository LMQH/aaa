"""Agent 装配：把 Provider、Tools、Context、Boundary、Memory 与持久化拼成 Agent。"""

from pathlib import Path
from typing import Any, List, Optional, Protocol, Sequence

from agno.agent import Agent
from agno.compaction import Compaction
from agno.db.sqlite import SqliteDb
from agno.memory import MemoryManager
from agno.models.base import Model
from agno.skills import Skills
from agno.tools.function import Function

from ..boundary.policy import MODE_READ, PolicySource, resolve_policy
from ..config.paths import DATA_DIR, DB_FILE
from ..context.params import compact_threshold, compaction_budget, history_runs
from ..memory import MemoryEngineError
from ..memory.host import ActiveMemory, MemoryHost
from ..model.catalog import Catalog, ModelRef
from ..plugins import GatedSkills
from ..tools.assemble import build_tools, tool_names
from .delegation import make_delegation_tools
from .delegation_registry import DelegationRegistry
from .prompt import instructions_for, project_instructions


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...


def tools_for_mode(mode: str, settings: SettingsLike, policy: PolicySource = resolve_policy) -> list:
    """权限模式与工具能力的合并点。

    `policy` 是替换点：默认用 `boundary.resolve_policy`；沙箱或插件可以传入
    自己的实现，返回不同的能力清单与隔离范围，而不用改工具或运行时。
    """
    resolved = policy(mode)
    return build_tools(
        resolved.capabilities,
        root=resolved.isolation.file_root,
        cwd=resolved.isolation.command_cwd,
        confirm_tools=resolved.confirm_tools,
        file_caps={
            "max_file_lines": int(settings.get("tools.file_max_lines")),
            "max_file_length": int(settings.get("tools.file_max_length")),
        },
        weather_timeout=int(settings.get("tools.weather_timeout")),
        shell_timeout=int(settings.get("tools.shell_timeout")),
        shell_output_bytes=int(settings.get("tools.shell_output_bytes")),
        shell_tail=int(settings.get("tools.shell_tail")),
    )


def _extension_tools(extra_tools: Optional[List]) -> list:
    return list(extra_tools or ())


def skills_for_mode(skills: Optional[Skills], mode: str) -> Optional[Skills]:
    """read-only 不开放技能脚本执行（owner 2026-11-24 决定）：换用无脚本变体。

    非 Skills 输入原样返回，兼容测试替身。
    """
    if not isinstance(skills, Skills) or mode != MODE_READ:
        return skills
    return GatedSkills(skills.loaders, allow_scripts=False)


def _session_db() -> SqliteDb:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return SqliteDb(db_file=str(DB_FILE))


def _memory_tool_entrypoint(memory: MemoryHost, name: str):
    """工具入口：Agno 注入当轮 RunContext，aaa 只把窄上下文交给引擎。"""

    def _call(_agno_run_context=None, **arguments):
        identity = memory.identity(
            user_id=getattr(_agno_run_context, "user_id", None),
            session_id=getattr(_agno_run_context, "session_id", None),
            run_id=memory.continued_run_id.get() or getattr(_agno_run_context, "run_id", None),
        )
        return memory.call_tool(name, arguments, identity)

    return _call


def _memory_tool_functions(memory: ActiveMemory, existing: list) -> list:
    """把外置引擎声明的工具包装成 Agno Function；与现有工具重名时拒绝装配。"""
    if not isinstance(memory, MemoryHost):
        return []
    functions: list = []
    taken = set(tool_names(existing))
    for spec in memory.tool_specs():
        if spec.name in taken:
            raise MemoryEngineError(f"记忆工具与现有工具重名：{spec.name}")
        taken.add(spec.name)
        functions.append(Function(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters or {"type": "object", "properties": {}, "required": []},
            entrypoint=_memory_tool_entrypoint(memory, spec.name),
            requires_confirmation=spec.requires_confirmation,
            skip_entrypoint_processing=True,
        ))
    return functions


def _with_memory_guidance(memory: MemoryHost, instructions: str) -> str:
    """把工具清单和引擎提示块追加到 system prompt；引擎不改宿主模板。"""
    extras: list = []
    specs = memory.tool_specs()
    if specs:
        lines = "\n".join(f"- {spec.name}: {spec.description}".rstrip() for spec in specs)
        extras.append(f"<memory_tools>\n{lines}\n</memory_tools>")
    block = memory.prompt_block()
    if block:
        extras.append(block)
    if not extras:
        return instructions
    return f"{instructions}\n\n" + "\n\n".join(extras)


def _host_instructions(memory: MemoryHost, instructions: str) -> str:
    """外置记忆的提示词合成：工具清单 + 引擎提示块 + 宿主召回模板。"""
    return memory.instructions(_with_memory_guidance(memory, instructions))


def system_instructions(
    mode: str, tools: list, memory: ActiveMemory, *, project_max_chars: int = 0
) -> str:
    """系统提示词正文（技能索引由 Agno skills 注入，不在这里）；build_agent 与 /insight 同源。"""
    instructions = instructions_for(mode, tool_names(tools), project_instructions(project_max_chars))
    if isinstance(memory, MemoryHost):
        instructions = _host_instructions(memory, instructions)
    return instructions


def _memory_options(
    memory: ActiveMemory, settings: SettingsLike, model: Model, db: SqliteDb, instructions: str
) -> dict:
    """调用方须传入 system_instructions 的合成结果；这里不再重复追加记忆引导。"""
    if memory == "builtin":
        return {
            "instructions": instructions,
            "memory_manager": MemoryManager(
                model=model,
                db=db,
                delete_memories=bool(settings.get("memory.delete_memories")),
                clear_memories=bool(settings.get("memory.clear_memories")),
            ),
            "enable_agentic_memory": True,
            "add_memories_to_context": True,
        }
    options = {"instructions": instructions, "add_memories_to_context": False}
    if isinstance(memory, MemoryHost):
        options.update(
            pre_hooks=[memory.recall_hook],
            post_hooks=[memory.observe_hook],
        )
    return options


def build_agent(
    catalog: Catalog,
    provider: str,
    model_id: str,
    mode: str,
    settings: SettingsLike,
    extra_tools: Optional[List] = None,
    *,
    skills: Optional[Skills] = None,
    memory: ActiveMemory = None,
    delegation: Optional[DelegationRegistry] = None,
    delegation_models: Sequence[ModelRef] = (),
) -> Agent:
    """构造 Agent 实例：Provider + Tools + 会话持久化 + 原生压缩 + 记忆。

    切换模型、权限模式或相关配置都重建 Agent，不做原地改：Agno 只在构造时
    把 fallback_models 转成 fallback_config，原地改两个字段迟早漏掉一个。
    """
    model = catalog.require(provider, model_id)[1]

    fallback_models = catalog.fallbacks(provider, model_id)
    compaction = Compaction(
        compact_at_tokens=compact_threshold(model.context_window, settings),
        uncompacted_runs=history_runs(settings),
        compacted_token_budget=compaction_budget(settings),
        store_compacted_messages=True,
        search_compacted_messages=True,
        on_context_overflow=True,
        enforce_min_fold_ratio=True,
    )
    all_tools = tools_for_mode(mode, settings) + _extension_tools(extra_tools)
    if delegation is not None:
        all_tools += make_delegation_tools(
            delegation, catalog, delegation_models, ModelRef(provider, model_id),
            lambda: tools_for_mode(mode, settings), settings,
        )
    all_tools += _memory_tool_functions(memory, all_tools)

    model_instance = catalog.build(provider, model_id)
    db = _session_db()
    instructions = system_instructions(
        mode, all_tools, memory,
        project_max_chars=int(settings.get("project.instructions_max_chars")),
    )
    memory_options = _memory_options(memory, settings, model_instance, db, instructions)

    return Agent(
        model=model_instance,
        fallback_models=fallback_models or None,
        compaction=compaction,
        # 超过 16k 字符的工具结果落 AgentFS，上下文只留信封与回读方式。
        offload_tool_results=True,
        # MCP 桥接作为常规工具传给 Agno；技能索引与读取工具由 Agno skills 原生装配。
        tools=all_tools,
        skills=skills_for_mode(skills, mode),
        **memory_options,
        # 内置记忆沿用会话库；外置引擎不接触该库。
        db=db,
        # Agno 把 session metadata 写进会话记录；`-c` 据此只续接当前目录的会话。
        metadata={"cwd": str(Path.cwd())},
        # 把同一 session 的历史消息带进下一轮；轮数上限只是防止无限增长。
        add_history_to_context=True,
        num_history_runs=history_runs(settings),
        markdown=False,
        telemetry=False,
    )
