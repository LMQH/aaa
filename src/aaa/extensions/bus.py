"""事件总线：订阅、发射与异常隔离。

- handler 同步调用；单次异常记日志并跳过，同一扩展累计 3 次 → 本会话挂起。
- 发射永不向调用方抛异常：事件是旁路观察，不能影响主循环。
"""

import logging
from collections import defaultdict
from typing import Callable, Dict, List, Set

from .protocol import Event

logger = logging.getLogger(__name__)

SUSPEND_AFTER_FAILURES = 3


class EventBus:
    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[Event], None]]] = defaultdict(list)
        self._owner_of: Dict[Callable[[Event], None], str] = {}
        self._failures: Dict[str, int] = defaultdict(int)
        self._suspended: Set[str] = set()

    def subscribe(self, event: str, handler: Callable[[Event], None], *, owner: str) -> None:
        self._subs[event].append(handler)
        self._owner_of[handler] = owner

    def emit(self, event: str, payload: Dict | None = None) -> None:
        """向订阅者派发事件；任何异常都被吞掉并计入挂起计数。"""
        event_obj = Event(name=event, payload=payload or {})
        for handler in list(self._subs.get(event, ())):
            owner = self._owner_of.get(handler, "")
            if owner and owner in self._suspended:
                continue
            try:
                handler(event_obj)
            except Exception:
                logger.warning("扩展 %s 的 %s 事件处理失败", owner or "未知", event, exc_info=True)
                if owner:
                    self._failures[owner] += 1
                    if self._failures[owner] >= SUSPEND_AFTER_FAILURES:
                        self._suspended.add(owner)
                        logger.warning("扩展 %s 本会话内挂起（事件处理连续失败）", owner)

    def suspend(self, owner: str, reason: str) -> None:
        """加载/激活失败时的会话内挂起：事件不再派发。"""
        self._suspended.add(owner)
        logger.warning("扩展 %s 挂起：%s", owner, reason)

    def suspended(self) -> Set[str]:
        return set(self._suspended)
