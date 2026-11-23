"""持久任务队列：单后台线程、SQLite 任务表、重启续跑。

工具与观察只负责入队；复制、抽取、摘要由 worker 在后台完成。任务状态持久化，
进程退出后未完成任务在下次启动继续。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("aaa.memory.miniviking")

MAX_ATTEMPTS = 2


class Tasks:
    def __init__(self, path: Path, handler: Callable[[dict], None], *, close_timeout: float = 3.0) -> None:
        self._handler = handler
        self._close_timeout = max(float(close_timeout), 0.3)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,"
                "payload TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',"
                "attempts INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL)"
            )
            self._conn.execute(
                "UPDATE tasks SET status='pending', updated=? WHERE status='running'",
                (int(time.time()),),
            )
        self._thread = threading.Thread(target=self._loop, name="miniviking-tasks", daemon=True)
        self._thread.start()

    def enqueue(self, kind: str, payload: dict) -> str:
        task_id = uuid.uuid4().hex[:16]
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks(id, kind, status, payload, created, updated) VALUES(?,?,?,?,?,?)",
                (task_id, kind, "pending", json.dumps(payload, ensure_ascii=False), now, now),
            )
        return task_id

    def status(self, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while time.monotonic() < deadline:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM tasks WHERE status IN ('pending','running') LIMIT 1"
                ).fetchone()
            if row is None:
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        self._stop.set()
        self._thread.join(self._close_timeout)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status='pending', updated=? WHERE status='running'",
                (int(time.time()),),
            )
        try:
            self._conn.close()
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            task = self._claim()
            if task is None:
                self._stop.wait(0.2)
                continue
            try:
                result = self._handler(task)
            except Exception as exc:
                self._fail(task, exc)
            else:
                try:
                    self._finish(task["id"], result)
                except sqlite3.Error:
                    pass

    def _claim(self) -> Optional[dict]:
        now = int(time.time())
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY created, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE tasks SET status='running', updated=? WHERE id=?", (now, row["id"])
            )
        task = dict(row)
        task["status"] = "running"
        return task

    def _finish(self, task_id: str, message: object = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status='done', message=?, updated=? WHERE id=? AND status='running'",
                (str(message or "")[:500], int(time.time()), task_id),
            )

    def _fail(self, task: dict, exc: Exception) -> None:
        attempts = int(task.get("attempts") or 0) + 1
        status = "pending" if attempts < MAX_ATTEMPTS and not self._stop.is_set() else "failed"
        message = f"{type(exc).__name__}: {exc}"[:500]
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE tasks SET status=?, attempts=?, message=?, updated=?"
                    " WHERE id=? AND status='running'",
                    (status, attempts, message, int(time.time()), task["id"]),
                )
        except sqlite3.Error:
            return
        if status == "failed":
            logger.warning("miniviking 任务失败（%s）：%s", task.get("kind"), message)
