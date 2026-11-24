"""编程式扩展的发现、导入、激活与注册表。

发现约定：`extensions/<name>/extension.toml` 的 `[extension]` 段声明
`kind = "python"`，同目录 `extension.py` 导出 `activate(ctx)`，可选
`PROTOCOL_VERSION`（不得高于 aaa 当前版本）。加载失败只禁用该扩展。
"""

import importlib.util
import logging
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Dict, List, Optional, Tuple

from agno.tools.function import Function

from ..boundary.policy import resolve_policy
from .bus import SUSPEND_AFTER_FAILURES, EventBus
from .protocol import (
    CURRENT_PROTOCOL_VERSION,
    CommandSpec,
    ExtensionContext,
    PanelSpec,
    PromptBlockSpec,
    RenderInfo,
    ToolCall,
    ToolDecision,
    ToolSpec,
)

logger = logging.getLogger(__name__)


@dataclass
class PythonExtension:
    name: str
    path: Path
    source: str


@dataclass
class ExtensionRegistry:
    """会话级注册表：工具声明、提示词块、挂起名单与事件总线。"""

    bus: EventBus = field(default_factory=EventBus)
    tools: List[ToolSpec] = field(default_factory=list)
    blocks: List[PromptBlockSpec] = field(default_factory=list)
    panels: List[PanelSpec] = field(default_factory=list)
    commands: List[CommandSpec] = field(default_factory=list)
    # 工具调用前的拦截器：(扩展名, handler)；异常/超时即拒绝并计数。
    interceptors: List[Tuple[str, Callable[[ToolCall], object]]] = field(default_factory=list)
    interceptor_failures: Dict[str, int] = field(default_factory=dict)
    loaded: List[str] = field(default_factory=list)
    disabled: List[str] = field(default_factory=list)
    active_panel: Optional[str] = None

    def register_tool(self, spec: ToolSpec) -> None:
        self.tools.append(spec)

    def register_block(self, spec: PromptBlockSpec) -> None:
        self.blocks.append(spec)

    def register_interceptor(self, owner: str, handler: Callable[[ToolCall], object]) -> None:
        self.interceptors.append((owner, handler))

    def register_command(self, spec: CommandSpec) -> None:
        if any(c.owner == spec.owner and c.name == spec.name for c in self.commands):
            raise ValueError(f"命令重复：{spec.name}（扩展 {spec.owner}）")
        self.commands.append(spec)

    def register_panel(self, spec: PanelSpec) -> None:
        # 面板名自动加扩展名前缀：不同扩展的同名面板可以共存。
        full = f"{spec.owner}/{spec.name}"
        if any(p.name == full for p in self.panels):
            raise ValueError(f"面板名重复：{full}")
        self.panels.append(PanelSpec(owner=spec.owner, name=full, title=spec.title, render=spec.render))

    def resolve_panel(self, name: str) -> Optional[str]:
        """把完整名或唯一短名解析成完整名；不存在或短名有歧义返回 None。"""
        if any(p.name == name for p in self.panels):
            return name
        short = [p.name for p in self.panels if p.name.split("/", 1)[-1] == name]
        return short[0] if len(short) == 1 else None

    def activate_panel(self, name: Optional[str]) -> bool:
        """切换当前面板；`None` 关闭。名字不存在或短名有歧义时返回 False。"""
        full = None if name is None else self.resolve_panel(name)
        if name is not None and full is None:
            return False
        self.active_panel = full
        return True

    def panel(self, name: str) -> Optional[PanelSpec]:
        return next((p for p in self.panels if p.name == name), None)

    def render_blocks(self, mode: str, cap: int) -> str:
        """渲染全部提示词块；`cap` 为单块字符上限，0 表示关闭。"""
        if cap <= 0 or not self.blocks:
            return ""
        parts: List[str] = []
        for spec in self.blocks:
            try:
                text = spec.render(RenderInfo(mode=mode, extension=spec.owner)).strip()
            except Exception:
                logger.warning("扩展 %s 的提示词块 %s 渲染失败，跳过", spec.owner, spec.name, exc_info=True)
                continue
            if not text:
                continue
            if len(text) > cap:
                text = text[: max(0, cap - 16)].rstrip() + "\n…(已截断)"
            parts.append(f'<extension_block source="{spec.owner}/{spec.name}">\n{text}\n</extension_block>')
        return "\n\n".join(parts)

    def suspend(self, owner: str, reason: str) -> None:
        self.bus.suspend(owner, reason)

    def tools_for(self, capabilities: Collection[str], mode: str) -> List[Function]:
        """按权限模式装配工具：既有能力名跟随能力集合，自定义能力默认
        `workspace-write` 以上可得（`read_safe` 全模式）；确认规则按模式落
        `requires_confirmation`。"""
        out: List[Function] = []
        for spec in self.tools:
            if not _granted(spec, capabilities):
                continue
            fn = Function.from_callable(spec.fn)
            fn.name = spec.name
            fn.requires_confirmation = _requires_confirmation(spec, mode)
            out.append(fn)
        return out


def _granted(spec: ToolSpec, capabilities: Collection[str]) -> bool:
    if spec.capability in capabilities:
        return True
    return spec.read_safe or "shell" in capabilities


def _requires_confirmation(spec: ToolSpec, mode: str) -> bool:
    """`always`/`dangerous` 每次确认；`mode` 跟随当前模式的确认名单——
    read-only 与 danger-full-access 的名单为空则不问，workspace-write 询问。"""
    if spec.confirm == "mode":
        return bool(resolve_policy(mode).confirm_tools)
    return True


def discover_python_extensions(dirs) -> List[PythonExtension]:
    """按来源顺序发现 `kind = "python"` 的扩展；同名先到先得。"""
    out: List[PythonExtension] = []
    seen: set = set()
    for d, source in dirs:
        if not Path(d).is_dir():
            continue
        for child in sorted(Path(d).iterdir()):
            if not child.is_dir() or child.name in seen:
                continue
            manifest = child / "extension.toml"
            if not manifest.is_file():
                continue
            try:
                parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                logger.warning("扩展 %s 的 extension.toml 不可读，跳过", child.name)
                continue
            if not isinstance(parsed.get("extension"), dict) or parsed["extension"].get("kind") != "python":
                continue
            if not (child / "extension.py").is_file():
                logger.warning("扩展 %s 声明 python 但缺少 extension.py，跳过", child.name)
                continue
            seen.add(child.name)
            out.append(PythonExtension(child.name, child, source))
    return out


def load_extensions(
    extensions: List[PythonExtension], registry: ExtensionRegistry, settings=None,
) -> None:
    """逐个导入并激活；任何失败只禁用该扩展（会话内挂起 + 记名单）。"""
    for ext in extensions:
        try:
            _activate(ext, registry, settings)
            registry.loaded.append(ext.name)
        except Exception as exc:
            registry.disabled.append(ext.name)
            registry.suspend(ext.name, f"加载失败：{exc}")
            logger.warning("扩展 %s 加载失败：%s", ext.name, exc)


def _activate(ext: PythonExtension, registry: ExtensionRegistry, settings=None) -> None:
    spec = importlib.util.spec_from_file_location(f"aaa_ext_{ext.name}", ext.path / "extension.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法构造导入器：{ext.path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    version = getattr(module, "PROTOCOL_VERSION", CURRENT_PROTOCOL_VERSION)
    if not isinstance(version, int) or version > CURRENT_PROTOCOL_VERSION:
        raise ValueError(f"协议版本不受支持：{version!r}（当前 {CURRENT_PROTOCOL_VERSION}）")
    activate = getattr(module, "activate", None)
    if not callable(activate):
        raise ValueError("缺少 activate(ctx) 入口")
    activate(ExtensionContext(
        registry, registry.bus, ext.name, protocol_version=CURRENT_PROTOCOL_VERSION,
        settings=settings, home=str(ext.path),
    ))


def extension_tools(registry: Optional[ExtensionRegistry], capabilities: Collection[str], mode: str) -> List[Function]:
    return registry.tools_for(capabilities, mode) if registry is not None else []


def _normalize_decision(result: object) -> ToolDecision:
    if result is None or result is True:
        return ToolDecision.allow()
    if result is False:
        return ToolDecision.deny("扩展拦截器拒绝")
    if isinstance(result, ToolDecision):
        return result
    raise TypeError(f"拦截器须返回 None/bool/ToolDecision，收到 {type(result).__name__}")


def _run_bounded(handler: Callable[[ToolCall], object], call: ToolCall, timeout: float) -> object:
    """在工作线程里执行拦截器并限时；超时抛 TimeoutError（线程本身无法终止）。"""
    box: list = []

    def target() -> None:
        try:
            box.append(handler(call))
        except BaseException as exc:  # 由调用方统一转成拒绝
            box.append(exc)

    worker = threading.Thread(target=target, name="aaa-ext-hook", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"拦截器超过 {timeout:g} 秒未返回")
    if box and isinstance(box[0], BaseException):
        raise box[0]
    return box[0] if box else None


def tool_guard(registry: "ExtensionRegistry", timeout: float) -> Callable:
    """生成挂到 Agno `tool_hooks` 的守门钩子：工具调用前依次询问扩展拦截器。

    只能收紧：拒绝以说明作为工具结果短路，不改确认流程；不支持改参（Agno 的
    确认暂停在钩子之前，改参会绕过用户已确认的内容）。异常或超时视为拒绝并
    可见；同一扩展累计 3 次失败后会话内挂起，其拦截器随之跳过。
    """

    def guard(function_call, arguments, name: str):
        call = ToolCall(name=name, args=dict(arguments or {}))
        for owner, handler in list(registry.interceptors):
            if owner in registry.bus.suspended():
                continue
            try:
                decision = _normalize_decision(_run_bounded(handler, call, timeout))
            except Exception as exc:
                decision = ToolDecision.deny(f"扩展 {owner} 的拦截器失败：{exc}")
                registry.interceptor_failures[owner] = registry.interceptor_failures.get(owner, 0) + 1
                if registry.interceptor_failures[owner] >= SUSPEND_AFTER_FAILURES:
                    registry.suspend(owner, "工具拦截器连续失败")
            if not decision.allow:
                logger.warning("扩展 %s 拒绝工具 %s：%s", owner, name, decision.reason)
                return f"已拒绝执行 {name}：{decision.reason or '扩展拦截器拒绝'}"
        return function_call(**arguments)

    return guard
