"""计价纯函数：四类 token × 单价 × 峰谷乘数。

单位约定：`Cost` 的单价是「美元 / 百万 token」，`cost_of` 返回美元。
时间只用于峰谷：`off_peak.windows` 是本地的当天分钟数窗口。
"""

from datetime import datetime
from typing import Optional

from ..model.provider import Cost

TOKENS_PER_UNIT = 1_000_000


def in_off_peak(cost: Cost, at: datetime) -> bool:
    """`at`（本地时间）是否落在低谷窗口内；窗口可跨午夜。"""
    if not cost.off_peak or not cost.off_peak.windows:
        return False
    minute = at.hour * 60 + at.minute
    for start, end in cost.off_peak.windows:
        if start == end:
            continue
        if start < end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # 跨午夜
            return True
    return False


def multiplier(cost: Cost, at: datetime) -> float:
    """该时刻适用的价格乘数：低谷 0.5 这类，其余为 1。"""
    return cost.off_peak.multiplier if in_off_peak(cost, at) else 1.0


def cost_of(
    cost: Optional[Cost],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    at: datetime,
) -> Optional[float]:
    """一次用量按给定价目折算成美元；无价目时返回 None（记为未定价）。"""
    if cost is None:
        return None
    factor = multiplier(cost, at) / TOKENS_PER_UNIT
    return (
        cost.input * input_tokens
        + cost.output * output_tokens
        + cost.cache_read * cache_read_tokens
        + cost.cache_write * cache_write_tokens
    ) * factor
