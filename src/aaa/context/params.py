"""Context 组件的参数：历史轮数、压缩阈值与压缩预算，全部来自配置。

只提供派生规则；压缩的触发与折叠由 Agno 执行。阈值同时被状态条使用，
保证界面显示的预留位置与实际压缩行为只有一个来源。
"""

from typing import Any, Protocol


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...


def history_runs(settings: SettingsLike) -> int:
    """保留尾轮数：折叠时最近 N 轮原文不进摘要。

    设了压缩阈值后压缩接管回放窗口，这个值不再限制回放，只决定保留尾大小。
    """
    return int(settings.get("context.history_runs"))


def compaction_budget(settings: SettingsLike) -> int:
    """压缩后保留的 token 预算。"""
    return int(settings.get("context.compaction_budget"))


def compact_threshold(window: int, settings: SettingsLike) -> int:
    """压缩阈值：窗口减去预留量。预留是窗口的 (1 - ratio)，且不小于 reserve。"""
    ratio = float(settings.get("context.threshold_ratio"))
    reserve = int(settings.get("context.threshold_reserve"))
    # Agno 要求阈值是正整数；预留量吃掉整个窗口时退到 1，而不是 0。
    return max(1, window - max(int(window * (1.0 - ratio)), reserve))
