"""用量报表：从账本聚合「渠道 × 模型」的 token 与金额。

只读账本，不写任何库；金额已在入账时冻结，这里只做求和与排序。
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from ..config.paths import USAGE_FILE

PERIODS = ("day", "week", "month")

SELECT = """
SELECT channel, model_id,
       SUM(input_tokens), SUM(output_tokens),
       SUM(cache_read_tokens), SUM(cache_write_tokens),
       SUM(cost_total),
       SUM(CASE WHEN cost_total IS NULL THEN 1 ELSE 0 END),
       COUNT(*)
FROM usage_events
WHERE created_at >= ? AND created_at <= ?
GROUP BY channel, model_id
ORDER BY SUM(input_tokens + output_tokens) DESC
"""


@dataclass(frozen=True)
class Row:
    """一个 (渠道, 模型) 的合计；`cost` 为 None 表示这批记录都未定价。"""

    channel: Optional[str]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: Optional[float] = None
    unpriced: int = 0
    requests: int = 0

    @property
    def tokens(self) -> int:
        """排序与「合计」用的口径：输入 + 输出。"""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Report:
    period: str
    start: datetime
    end: datetime
    rows: List[Row] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(row.tokens for row in self.rows)

    @property
    def input_tokens(self) -> int:
        return sum(row.input_tokens for row in self.rows)

    @property
    def output_tokens(self) -> int:
        return sum(row.output_tokens for row in self.rows)

    @property
    def cache_read_tokens(self) -> int:
        return sum(row.cache_read_tokens for row in self.rows)

    @property
    def cost(self) -> Optional[float]:
        """有价的行之和；全部未定价时为 None。"""
        priced = [row.cost for row in self.rows if row.cost is not None]
        return sum(priced) if priced else None

    @property
    def unpriced(self) -> int:
        return sum(row.unpriced for row in self.rows)

    @property
    def requests(self) -> int:
        return sum(row.requests for row in self.rows)


def window(period: str, now: Optional[datetime] = None):
    """本地时间的自然日 / 自然周（周一起）/ 自然月起点与当前时刻。"""
    now = now or datetime.now()
    if period not in PERIODS:
        raise KeyError(f"未知统计周期：{period}")
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    elif period == "month":
        start = start.replace(day=1)
    return start, now


def report(
    period: str = "day",
    now: Optional[datetime] = None,
    usage_db: Path = USAGE_FILE,
) -> Report:
    """按周期聚合账本；账本不存在时返回空报表。"""
    start, end = window(period, now)
    if not Path(usage_db).exists():
        return Report(period=period, start=start, end=end)
    connection = sqlite3.connect(str(usage_db), timeout=10)
    try:
        rows = [
            Row(
                channel=channel,
                model=model_id,
                input_tokens=inp or 0,
                output_tokens=out or 0,
                cache_read_tokens=read or 0,
                cache_write_tokens=write or 0,
                cost=cost,
                unpriced=unpriced or 0,
                requests=requests or 0,
            )
            for channel, model_id, inp, out, read, write, cost, unpriced, requests in
            connection.execute(SELECT, (int(start.timestamp()), int(end.timestamp())))
        ]
    except sqlite3.OperationalError:  # 表还没建出来
        rows = []
    finally:
        connection.close()
    return Report(period=period, start=start, end=end, rows=rows)
