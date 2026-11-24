"""扩展协议（阶段 1：协议内核）。

设计边界见 DESIGN.md 与 docs/architecture.md「扩展协议」节：扩展只挂装配点
与 aaa 自有层，不触 Agno 执行路径；协议归 aaa 翻译。

- `protocol`：扩展可见的全部接口（`ExtensionContext`、事件目录、版本常量）。
- `bus`：事件总线（订阅/发射/异常隔离/挂起）。
- `loader`：`extensions/<name>/` 的发现、导入、激活与失败隔离。
"""

from typing import Dict, Optional

from .bus import EventBus
from .loader import ExtensionRegistry, discover_python_extensions, load_extensions
from .protocol import CURRENT_PROTOCOL_VERSION, Event, ExtensionContext

__all__ = [
    "CURRENT_PROTOCOL_VERSION",
    "Event",
    "ExtensionContext",
    "EventBus",
    "ExtensionRegistry",
    "discover_python_extensions",
    "load_extensions",
    "set_default_registry",
    "emit_run_failed",
]

_default_registry: Optional[ExtensionRegistry] = None


def set_default_registry(registry: Optional[ExtensionRegistry]) -> None:
    """宿主启动时登记默认注册表；CLI 失败路径经它发 run.failed。"""
    global _default_registry
    _default_registry = registry


def emit_run_failed(payload: Dict) -> None:
    if _default_registry is not None:
        _default_registry.bus.emit("run.failed", payload)
