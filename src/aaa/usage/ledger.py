"""用量账本：把 Agno 的 run metrics 增量同步到独立的 `usage.db`。

为什么独立：账本是派生数据，删掉能重建；它写自己的文件，不碰 Agno 的表，也不
在运行期争 `sessions.db` 的写锁。

成本在**写入时**按当时价目算好并冻结（改价不影响历史）。唯一的例外是未定价的
行：下次同步时若已配出价，会补算一次；已有金额永不被改写。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..config.paths import DB_FILE, USAGE_FILE
from ..model.provider import Cost
from .cost import cost_of

logger = logging.getLogger(__name__)

# 只有终态 run 入账：PAUSED/RUNNING/PENDING 的 metrics 还在长。
TERMINAL_STATUSES = ("COMPLETED", "CANCELLED", "ERROR", "REGENERATED")

# 取价：`(渠道, 模型) -> 价目 | None`。由调用方注入，本模块不认识 Catalog。
PriceSource = Callable[[Optional[str], str], Optional[Cost]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    run_id             TEXT NOT NULL,
    model_type         TEXT NOT NULL,
    channel            TEXT,
    model_id           TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    cwd                TEXT,
    created_at         INTEGER NOT NULL,
    run_updated_at     INTEGER NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    reasoning_tokens   INTEGER NOT NULL,
    cost_total         REAL,
    PRIMARY KEY (run_id, model_type)
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_channel ON usage_events(channel, model_id);
"""

INSERT = """
INSERT INTO usage_events (
    run_id, model_type, channel, model_id, session_id, cwd, created_at, run_updated_at,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
    cost_total
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# 只按原生 run 入账，不重复读取会话级汇总。
SELECT_RUNS = """
SELECT run_id, session_id, status, created_at, updated_at, run_data
FROM agno_runs
WHERE status IN ({})
"""


@dataclass(frozen=True)
class SyncResult:
    """一次同步的结果：新增/更新几个 run，补算了几行未定价记录。"""

    added: int = 0
    updated: int = 0
    backfilled: int = 0

    @property
    def touched(self) -> int:
        return self.added + self.updated


def sync(
    price: Optional[PriceSource] = None,
    sessions_db: Path = DB_FILE,
    usage_db: Path = USAGE_FILE,
) -> SyncResult:
    """把新出现或更新过的终态 run 写进账本，并补算未定价的行。"""
    if not Path(sessions_db).exists():
        return SyncResult()
    usage_db = Path(usage_db)
    usage_db.parent.mkdir(parents=True, exist_ok=True)

    ledger = sqlite3.connect(str(usage_db), timeout=10)
    try:
        ledger.executescript(SCHEMA)
        known = dict(ledger.execute("SELECT run_id, run_updated_at FROM usage_events"))
        source = _open_readonly(sessions_db)
        try:
            pending = [
                row for row in _runs(source)
                if row[0] not in known or row[4] > known[row[0]]
            ]
            cwds = _cwd_map(source)
        finally:
            source.close()

        added = updated = 0
        for run_id, session_id, status, created_at, updated_at, run_data in pending:
            channel, events = _events(run_data)
            if not events:
                continue
            cwd = cwds.get(session_id)
            ledger.execute("DELETE FROM usage_events WHERE run_id = ?", (run_id,))
            ledger.executemany(INSERT, [
                (run_id, model_type, channel, model_id, session_id, cwd,
                 created_at, updated_at, input_tokens, output_tokens,
                 cache_read, cache_write, reasoning,
                 _cost(price, channel, model_id, created_at,
                       input_tokens, output_tokens, cache_read, cache_write))
                for model_type, model_id, input_tokens, output_tokens, cache_read, cache_write, reasoning in events
            ])
            if run_id in known:
                updated += 1
            else:
                added += 1
        ledger.commit()

        backfilled = _backfill(ledger, price)
        ledger.commit()
        return SyncResult(added=added, updated=updated, backfilled=backfilled)
    finally:
        ledger.close()


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = Path(path).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _runs(source: sqlite3.Connection) -> Iterable[Tuple]:
    """终态 run，`run_data` 解成字典；坏行跳过，不让一条脏数据卡住整个账本。"""
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    try:
        rows = source.execute(SELECT_RUNS.format(placeholders), TERMINAL_STATUSES)
    except sqlite3.OperationalError as exc:  # 库还没有 Agno 的表
        logger.debug("读取 agno_runs 失败：%s", exc)
        return
    for row in rows:
        try:
            data = json.loads(row[5] or "{}")
        except (TypeError, ValueError):
            logger.warning("跳过无法解析的 run_data：%s", row[0])
            continue
        yield (row[0], row[1], row[2], row[3], row[4], data)


def _cwd_map(source: sqlite3.Connection) -> Dict[str, str]:
    """session_id → cwd；索引表不存在时返回空（旧库/未使用时）。"""
    try:
        rows = source.execute(
            "SELECT session_id, cwd FROM aaa_cwd_session ORDER BY seq"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {session_id: cwd for session_id, cwd in rows}


def _events(run_data: Dict) -> Tuple[Optional[str], List[Tuple]]:
    """一次 run 拆成「渠道 + 每个模型角色一行」；没有用量时返回空列表。"""
    if not isinstance(run_data, dict):
        return None, []
    metadata = run_data.get("metadata") or {}
    channel = metadata.get("channel") if isinstance(metadata, dict) else None

    metrics = run_data.get("metrics") or {}
    details = metrics.get("details") or {}
    events: List[Tuple] = []
    if isinstance(details, dict) and details:
        for model_type, entries in details.items():
            for index, entry in enumerate(entries or []):
                # A native role can list multiple models (e.g. after fallback). Keep
                # the first role key unchanged and put additional models in distinct
                # slots of the existing (run_id, model_type) primary key.
                slot = str(model_type) if not index else f"{model_type}:{index + 1}"
                event = _event(slot, str(entry.get("id") or ""), entry)
                if event:
                    events.append(event)
    if not events:
        event = _event("model", str(run_data.get("model") or ""), metrics)
        if event:
            events.append(event)
    return (channel if isinstance(channel, str) else None), events


def _event(model_type: str, model_id: str, metrics: Dict) -> Optional[Tuple]:
    tokens = (
        int(metrics.get("input_tokens") or 0),
        int(metrics.get("output_tokens") or 0),
        int(metrics.get("cache_read_tokens") or 0),
        int(metrics.get("cache_write_tokens") or 0),
    )
    if not any(tokens) or not model_id:
        return None
    return (model_type, model_id, *tokens, int(metrics.get("reasoning_tokens") or 0))


def _cost(
    price: Optional[PriceSource],
    channel: Optional[str],
    model_id: str,
    created_at: int,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> Optional[float]:
    if price is None or not channel:
        return None
    try:
        cost = price(channel, model_id)
    except Exception:  # 取价失败不该阻断入账
        logger.exception("取价失败：%s/%s", channel, model_id)
        return None
    return cost_of(cost, input_tokens, output_tokens, cache_read, cache_write,
                   datetime.fromtimestamp(created_at))


def _backfill(ledger: sqlite3.Connection, price: Optional[PriceSource]) -> int:
    """只补空：未定价且现在有价的行，按各自发生时刻的峰谷重算。"""
    if price is None:
        return 0
    rows = ledger.execute(
        "SELECT run_id, model_type, channel, model_id, created_at, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens"
        " FROM usage_events WHERE cost_total IS NULL AND channel IS NOT NULL"
    ).fetchall()
    filled = 0
    for run_id, model_type, channel, model_id, created_at, inp, out, read, write in rows:
        cost = price(channel, model_id)
        if cost is None:
            continue
        value = cost_of(cost, inp, out, read, write, datetime.fromtimestamp(created_at))
        if value is None:
            continue
        ledger.execute(
            "UPDATE usage_events SET cost_total = ? WHERE run_id = ? AND model_type = ?",
            (value, run_id, model_type),
        )
        filled += 1
    return filled
