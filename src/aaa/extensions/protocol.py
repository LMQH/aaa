"""扩展可见的协议接口：上下文句柄、事件目录与版本。

扩展只看到这一层；aaa 负责把它翻译到装配点（Agno 公开接口 + aaa 自有层）。
工具注册必须声明能力名与确认规则，经 `boundary` 进入权限模型；名字加
`ext_` 前缀进入外部信任面（每次询问、审批策略不自动放行）。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional

CURRENT_PROTOCOL_VERSION = 1
COMMAND_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# v1 冻结的事件目录；自定义事件走 `ext/<扩展名>/<事件>` 命名空间。
EVENTS = (
    "run.started",
    "run.completed",
    "run.failed",
    "tool.called",
    "tool.returned",
    "tool.confirmed",
    "message.user",
    "message.assistant",
    "compaction.done",
)

ConfirmRule = Literal["always", "dangerous", "mode"]


@dataclass(frozen=True)
class Event:
    """发给 handler 的事件；`name` 为目录内事件名或 `ext/<名>/<事件>`。"""

    name: str
    payload: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """一次工具注册的声明；装配时按权限模式过滤。"""

    owner: str
    fn: Callable
    name: str                      # ext_<owner>_<name>
    capability: str                # boundary 能力名，或 ext/<owner>/<name>
    confirm: ConfirmRule
    read_safe: bool


@dataclass(frozen=True)
class RenderInfo:
    """提示词块渲染时的上下文快照。"""

    mode: str
    extension: str


@dataclass(frozen=True)
class PromptBlockSpec:
    """一次提示词块注册的声明；渲染发生在每次装配系统消息时。"""

    owner: str
    name: str
    placement: str                  # v1 仅 "system_tail"
    render: Callable[[RenderInfo], str]


@dataclass(frozen=True)
class PanelSpec:
    """状态栏面板：`render` 收到状态快照 dict，返回行文本列表。"""

    owner: str
    name: str
    title: str
    render: Callable[[Dict], List[str]]


@dataclass(frozen=True)
class ToolCall:
    """一次即将执行的工具调用：工具名与参数。"""

    name: str
    args: Dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDecision:
    """拦截器的裁决：`allow()` 放行，`deny(reason)` 拒绝（理由对模型与用户可见）。"""

    allow: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "ToolDecision":
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> "ToolDecision":
        return cls(False, reason)


@dataclass(frozen=True)
class CommandSpec:
    """编程式扩展注册的斜杠命令：与 `commands/*.md` 模板同名字空间。"""

    owner: str
    name: str
    template: str


class SettingsView:
    """只读设置视图：只能读键表内的配置项，未知键返回默认值，没有写入口。"""

    def __init__(self, settings) -> None:
        self._settings = settings

    def get(self, path: str, default=None):
        try:
            return self._settings.get(path)
        except (AttributeError, KeyError, TypeError):
            return default


class ExtensionContext:
    """扩展激活时拿到的唯一句柄；能力收窄，不暴露 Agent 实例。"""

    def __init__(self, registry, bus, name: str, protocol_version: int = CURRENT_PROTOCOL_VERSION,
                 settings=None, home: Optional[str] = None):
        self._registry = registry
        self._bus = bus
        self.name = name
        self.protocol_version = protocol_version
        # 扩展自己的目录；settings 只读；日志带扩展名前缀并入 aaa 日志。
        self.home = home
        self.settings = SettingsView(settings)
        self.log = logging.getLogger(f"aaa.ext.{name}")

    def register_tool(
        self, fn: Callable, *, name: str, capability: str,
        confirm: ConfirmRule = "always", read_safe: bool = False,
    ) -> None:
        """注册一个工具。

        - `capability`：boundary 能力名（`read`/`write`/`shell`…）时跟随该能力
          在各权限模式的存在性；自定义名（建议 `ext/<扩展名>/<工具>`）默认
          `workspace-write` 以上可得，`read_safe=True` 才全模式可得。
        - `confirm`：`always` 每次询问；`mode` 跟随当前模式的确认名单（read-only 与
          danger-full-access 不问，workspace-write 询问）；`dangerous` v1 等同 `always`。
        """
        spec = ToolSpec(
            owner=self.name, fn=fn, name=f"ext_{self.name}_{name}",
            capability=capability, confirm=confirm, read_safe=read_safe,
        )
        self._registry.register_tool(spec)

    def subscribe(self, event: str, handler: Callable[[Event], None]) -> None:
        """订阅事件；目录内事件或 `ext/<扩展名>/<事件>` 自定义事件。"""
        self._bus.subscribe(event, handler, owner=self.name)

    def register_prompt_block(self, *, name: str, placement: Literal["system_tail"] = "system_tail",
                              render: Callable[[RenderInfo], str]) -> None:
        """注册提示词块：每次装配系统消息时渲染，追加在系统消息尾部。

        超过宿主字符上限的块会被截断；`placement` v1 仅 `system_tail`。
        """
        self._registry.register_block(PromptBlockSpec(
            owner=self.name, name=name, placement=placement, render=render,
        ))

    def register_panel(self, *, name: str, title: str,
                       render: Callable[[Dict], List[str]]) -> None:
        """注册状态栏面板；完整名为 `<扩展名>/<名>`，同一扩展内不可重名。"""
        self._registry.register_panel(PanelSpec(
            owner=self.name, name=name, title=title, render=render,
        ))

    def intercept_tool(self, handler: Callable[[ToolCall], object]) -> None:
        """注册工具调用前的拦截器：在每次工具执行前收到 `ToolCall`。

        返回 `None`/`True`/`ToolDecision.allow()` 放行，`False`/`ToolDecision.deny(理由)`
        拒绝——拒绝以说明作为工具结果返回给模型。只能收紧：拦截器不能放行需要
        确认的调用，也不能改参。异常或超时视为拒绝并可见；同一扩展累计 3 次失败后
        本会话内挂起。
        """
        self._registry.register_interceptor(self.name, handler)

    def register_command(self, name: str, template: str) -> None:
        """注册斜杠命令：与 `commands/*.md` 模板同名字空间、同 `$ARGUMENTS` 语义。

        与文件模板同名时文件模板优先；名称须为 `[a-z0-9][a-z0-9_-]*`，模板不能为空。
        """
        if not COMMAND_NAME_RE.fullmatch(name or ""):
            raise ValueError(f"命令名不合法：{name!r}（须为 [a-z0-9][a-z0-9_-]*）")
        if not template or not template.strip():
            raise ValueError(f"命令 {name} 的模板不能为空")
        self._registry.register_command(CommandSpec(owner=self.name, name=name, template=template))
