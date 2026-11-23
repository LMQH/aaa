"""按目录记住最近会话：自建索引表，与 Agno 的会话表同库。

`aaa_cwd_session(cwd PK, session_id, seq)`：`seq` 单调递增，用于在 Agno 的
`updated_at` 只有秒级精度时仍能定序。索引在库内，不再有文件系统旁路。
"""

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaa_cwd_session (
    cwd TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL
)
"""

# 写锁竞争时的等待上限（秒）：与 Agno 共用同一个库文件。
TIMEOUT_SECONDS = 5


def _connect(db_file: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_file), timeout=TIMEOUT_SECONDS)


def remember(db_file: Path, cwd: str, session_id: str) -> None:
    """记下某目录最近的会话；重复写入同一目录时覆盖并推进 seq。"""
    conn = _connect(db_file)
    try:
        conn.execute(SCHEMA)
        conn.execute(
            """
            INSERT INTO aaa_cwd_session (cwd, session_id, seq)
            VALUES (?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM aaa_cwd_session))
            ON CONFLICT(cwd) DO UPDATE SET
                session_id = excluded.session_id,
                seq = excluded.seq
            """,
            (cwd, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def latest(db_file: Path, cwd: str) -> Optional[str]:
    """该目录最近记录的会话 id；没有记录时返回 None。"""
    if not Path(db_file).exists():
        return None
    conn = _connect(db_file)
    try:
        row = conn.execute(
            "SELECT session_id FROM aaa_cwd_session WHERE cwd = ?", (cwd,)
        ).fetchone()
    except sqlite3.OperationalError:
        # 表还没建过：等同于没有记录。
        return None
    finally:
        conn.close()
    return row[0] if row else None


def forget(db_file: Path, cwd: str, session_id: str) -> None:
    """会话删除后清掉指向它的索引；不匹配（目录已指向别的会话）则不动。"""
    if not Path(db_file).exists():
        return
    conn = _connect(db_file)
    try:
        conn.execute(
            "DELETE FROM aaa_cwd_session WHERE cwd = ? AND session_id = ?",
            (cwd, session_id),
        )
        conn.commit()
    finally:
        conn.close()
