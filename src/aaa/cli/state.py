"""REPL 的可变状态。每个事实只有一个写入点。"""

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from agno.agent import Agent
from agno.session import AgentSession
from agno.skills import Skills

from .. import plugins, trust
from ..boundary.policy import resolve_policy
from ..config.loader import save_config
from ..config.paths import DELEGATIONS_DIR, SKILLS_DIRS
from ..config.settings import Settings, save
from ..extensions import ExtensionRegistry, load_extensions, set_default_registry
from ..memory import MemoryEngineError, MemoryHost, get_engine
from ..memory.host import ActiveMemory
from ..model.catalog import Catalog, ModelRef, from_config
from ..model.provider import Channel, ChannelModel
from ..runtime.agent import build_agent
from ..runtime.delegation_registry import DelegationRegistry
from ..sessions.query import new_session_id
from .completer import SlashCompleter
from .render import sanitize

_SOURCE_LABELS = {"project": "项目", "user": "用户", "builtin": "内置", "extension": "扩展"}


def _open_memory(settings: Settings) -> ActiveMemory:
    source = settings.get("memory.source")
    if source == "builtin":
        return "builtin"
    if source == "off":
        return None
    host = None
    engine = None
    try:
        if not source.startswith("external:"):
            raise MemoryEngineError(f"未知记忆引擎：{source}")
        engine_id = source.removeprefix("external:")
        extra_dir = trust.trusted_project_dir(settings, "memory_engines")
        engine = get_engine(
            engine_id, Path(settings.get("memory.data_root")).expanduser(), extra_dir=extra_dir
        )
        host = MemoryHost(engine, engine_id, settings)
        if not callable(getattr(engine, "recall", None)):
            raise MemoryEngineError(f"外置记忆引擎 {engine_id} 缺少 recall")
    except (Exception, SystemExit) as exc:
        closer = host if host is not None else engine
        if closer is not None and callable(getattr(closer, "close", None)):
            try:
                closer.close()
            except Exception:
                pass
        reason = str(exc) if isinstance(exc, MemoryEngineError) else type(exc).__name__
        message = f"{sanitize(reason)}；本次以记忆关闭状态启动。"
        logging.getLogger(__name__).warning("%s", message)
        print(message, file=sys.stderr)
        return None
    return host


@dataclass
class AppState:
    """一次进程内的全部可变状态：配置、模型、权限与当前会话执行体。"""

    config: Dict
    settings: Settings
    catalog: Catalog
    provider: str
    model_id: str
    mode: str
    runnable: Agent
    session_id: str
    completer: SlashCompleter
    memory: ActiveMemory = None
    context_tokens: Optional[int] = None
    # 本会话内已批准的工具调用签名（工具名 + 参数）；同一命令不再重复询问。
    approved: Set[str] = field(default_factory=set)
    # 用户在审批里选过「本会话总是允许」的工具名；按工具放行，会话结束即失效。
    tool_allows: Set[str] = field(default_factory=set)
    # 扩展点发现结果：命令模板、技能束、MCP 插件声明、桥接后的插件工具。
    plugin_commands: Dict[str, object] = field(default_factory=dict)
    plugin_skills: Optional[Skills] = None
    skill_sources: Dict[str, str] = field(default_factory=dict)
    # 技能包：技能名→包名（只含已装配的），以及 /plugin 用的包视图。
    skill_packs: Dict[str, str] = field(default_factory=dict)
    skill_pack_info: List = field(default_factory=list)
    plugin_extensions: List = field(default_factory=list)
    extension_registry: ExtensionRegistry = field(default_factory=ExtensionRegistry)
    extra_tools: List = field(default_factory=list)
    delegation: DelegationRegistry = field(default_factory=lambda: DelegationRegistry(DELEGATIONS_DIR))
    # 项目级扩展的加载摘要；仅交互启动时打印一行。
    project_note: str = ""
    # 交互报告收集器：非 None 时命令把（文本, 样式）追加到这里而不是打印。
    report_lines: Optional[List] = None

    @classmethod
    def start(cls, config: Dict, settings: Settings, trusted: frozenset = frozenset()) -> "AppState":
        """按归一后的配置构造主 Agent，发现扩展点。`trusted` 是已确认的仓库信任类别。"""
        catalog = from_config(config, settings)
        default = config["default"]
        provider, model_id = default["provider"], default["model"]
        mode = settings.get("boundary.default_mode")
        scope = catalog.scope(config.get("enabled_models") or [])

        project = plugins.project_dirs() if bool(settings.get("project.enabled")) else None
        discovery = plugins.discover_extensions(project, trusted=trusted, skills_user_dirs=SKILLS_DIRS)
        commands = discovery.commands
        skills, skill_sources = discovery.skills, discovery.skill_sources
        skill_packs = discovery.skill_packs
        extensions = discovery.mcp
        registry = ExtensionRegistry()
        load_extensions(discovery.python, registry, settings)
        set_default_registry(registry)
        # 编程式扩展注册的命令并入同一名字空间；文件模板优先，不覆盖已有命令。
        for spec in registry.commands:
            home = next((e.path for e in discovery.python if e.name == spec.owner), None)
            commands.setdefault(spec.name, plugins.CustomCommand(
                spec.name, home or Path("."), spec.template, "extension",
            ))
        source_labels = _SOURCE_LABELS
        project_names = [c.name for c in commands.values() if c.source == "project"]
        project_names += [n for n, s in skill_sources.items() if s == "project"]
        project_note = ""
        if project_names:
            shown = "、".join(project_names[:6]) + ("…" if len(project_names) > 6 else "")
            project_note = f"已加载项目级扩展 {len(project_names)} 项（{project['root']}）：{shown}"
        if registry.loaded:
            project_note += f"编程式扩展 {len(registry.loaded)} 个：{'、'.join(registry.loaded)}。"
        for name in registry.disabled:
            logging.getLogger(__name__).warning("编程式扩展 %s 加载失败已禁用", name)
        mcp_functions, mcp_failed = plugins.open_mcp_tools(
            extensions, plugins.mcp_timeout(settings)
        )
        for name in mcp_failed:
            logging.getLogger(__name__).warning("MCP 插件连接失败：%s", name)
        extra_tools = [*mcp_functions]
        extra_tools.extend(plugins.make_create_tools())

        session_id = new_session_id(settings)
        delegation = DelegationRegistry(DELEGATIONS_DIR)
        completer = SlashCompleter(
            [ref.display for ref in scope], settings,
            extra_commands=[
                (f"/{c.name}", f"[{source_labels[c.source]}] 自定义命令")
                for c in commands.values()
            ],
        )
        memory = _open_memory(settings)
        try:
            runnable = build_agent(
                catalog, provider, model_id, mode, settings, extra_tools=extra_tools, skills=skills,
                memory=memory, delegation=delegation, delegation_models=scope,
            )
        except BaseException:
            if isinstance(memory, MemoryHost):
                memory.close()
            raise

        state = cls(
            config=config,
            settings=settings,
            catalog=catalog,
            provider=provider,
            model_id=model_id,
            mode=mode,
            runnable=runnable,
            memory=memory,
            session_id=session_id,
            completer=completer,
            plugin_commands=commands,
            plugin_skills=skills,
            skill_sources=skill_sources,
            skill_packs=skill_packs,
            skill_pack_info=discovery.pack_info,
            plugin_extensions=extensions,
            extension_registry=registry,
            extra_tools=extra_tools,
            delegation=delegation,
            project_note=project_note,
        )
        return state

    def close_memory(self) -> None:
        memory, self.memory = self.memory, None
        if isinstance(memory, MemoryHost):
            memory.close()

    @property
    def model_ref(self) -> ModelRef:
        return ModelRef(self.provider, self.model_id)

    def selection(self) -> Tuple[Channel, ChannelModel]:
        """当前渠道与模型；一次查询，供标题生成等无状态调用使用。"""
        return self.catalog.require(self.provider, self.model_id)

    def window(self) -> int:
        """当前模型的上下文窗口；不缓存，避免与配置分叉。"""
        return self.catalog.window(self.provider, self.model_id)

    def select_session(self, session: AgentSession | None) -> None:
        """新建或续接 Agent 会话。"""
        session_id = session.session_id if session is not None else new_session_id(self.settings)
        if session_id == self.session_id:
            return
        self.session_id = session_id
        self.approved.clear()
        self.context_tokens = None

    def set_mode(self, mode: str) -> None:
        """权限模式与执行体一起生效；构造失败时保留原模式。"""
        previous = self.mode
        self.mode = mode
        try:
            self.rebuild_runnable()
        except Exception:
            self.mode = previous
            raise

    def rebuild_runnable(self) -> None:
        """按当前设置重建主 Agent。"""
        self.runnable = build_agent(
            self.catalog, self.provider, self.model_id, self.mode, self.settings,
            extra_tools=self.extra_tools, skills=self.plugin_skills, memory=self.memory,
            delegation=self.delegation, delegation_models=self.scope(),
            extensions=self.extension_registry,
        )
        self.completer = SlashCompleter(
            [ref.display for ref in self.scope()], self.settings,
            extra_commands=[
                (f"/{c.name}", f"[{_SOURCE_LABELS[c.source]}] 自定义命令")
                for c in self.plugin_commands.values()
            ],
        )
        self.context_tokens = None

    def scope(self) -> Tuple[ModelRef, ...]:
        """可切换范围：enabled_models 模式匹配后的模型。"""
        return self.catalog.scope(self.config.get("enabled_models") or [])

    def review_root(self) -> Path:
        """文件改动 diff 预览的根：当前模式的隔离声明，与工具装配同源。"""
        return resolve_policy(self.mode).isolation.file_root

    def set_model(self, provider: str, model_id: str, *, persist: bool = True) -> None:
        """切换模型：先确认存在，再重建，最后才落盘。"""
        self.catalog.require(provider, model_id)
        self.provider, self.model_id = provider, model_id
        self.rebuild_runnable()
        if persist:
            self.config["default"] = {"provider": provider, "model": model_id}
            save_config(self.config)

    def cycle_model(self, step: int) -> Optional[ModelRef]:
        """在可切换范围里前后移动；范围不足两个时返回 None。"""
        refs = list(self.scope())
        if len(refs) < 2:
            return None
        try:
            index = refs.index(self.model_ref)
        except ValueError:
            index = 0
        return refs[(index + step) % len(refs)]

    def apply_config(self, config: Dict) -> None:
        """渠道编辑后换掉配置与目录，回到新的默认模型。"""
        self.config = config
        self.catalog = from_config(config, self.settings)
        default = config["default"]
        self.set_model(default["provider"], default["model"], persist=False)

    def apply_settings(self) -> None:
        """配置改完：先落盘，再按生效方式重建。"""
        save(self.settings)
        self.catalog = from_config(self.config, self.settings)
        self.rebuild_runnable()
