"""SQLite 派生索引：trigram FTS5 检索 + `/memory` 分页。

索引可随时删除重建；写入失败只影响检索的及时性，事实仍在文件树里。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from .store import Node

logger = logging.getLogger("aaa.memory.miniviking")

SCHEMA_VERSION = "3"
SCOPE_KINDS = {
    "memory": ("memories", "sessions"),
    "resource": ("resources",),
    "skill": ("skills",),
    "all": ("memories", "sessions", "resources", "skills"),
}
LIST_KINDS = ("memories", "sessions", "resources", "skills")


@dataclass(frozen=True)
class Hit:
    uri: str
    kind: str
    title: str
    abstract: str
    tags: Tuple[str, ...]
    updated: int
    score: float


def _match(text: str) -> Optional[str]:
    """把查询转成 FTS 表达式：拉丁词按短语，中文按 3-gram 或。

    单个词不足 3 字符时返回 None，交给 LIKE 回退。
    """
    terms = [item for item in re.split(r"\s+", str(text or "").strip()) if item]
    if not terms:
        return None
    parts = []
    for term in terms:
        if len(term) < 3:
            return None
        if _is_cjk(term):
            grams = list(dict.fromkeys(term[index:index + 3] for index in range(len(term) - 2)))
            parts.append("(" + " OR ".join(f'"{gram}"' for gram in grams) + ")")
        else:
            escaped = term.replace('"', '""')
            parts.append(f'"{escaped}"')
    return " AND ".join(parts)


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _vec_rowid(text_hash: str) -> int:
    """由文本哈希派生的稳定整数 rowid：同哈希恒同 rowid，先删后插即 upsert。"""
    return int(text_hash[:16], 16) % (2 ** 63)


class Index:
    def __init__(self, path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._trigram = True
        self._vec_loaded: Optional[bool] = None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is not None and row["value"] != SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS fts")
                self._conn.execute("DROP TABLE IF EXISTS nodes")
                self._conn.execute("DROP TABLE IF EXISTS embeddings_meta")
                self._conn.execute("DELETE FROM meta WHERE key='schema'")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nodes("
                "uri TEXT PRIMARY KEY, user TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,"
                "title TEXT NOT NULL, title_key TEXT NOT NULL, abstract TEXT NOT NULL,"
                "overview TEXT NOT NULL, tags TEXT NOT NULL, keywords TEXT NOT NULL,"
                "created INTEGER NOT NULL, updated INTEGER NOT NULL, importance REAL NOT NULL,"
                "text_hash TEXT NOT NULL DEFAULT '')"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS nodes_owner ON nodes(user, agent, kind, updated)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings_meta("
                "hash TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,"
                "user TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,"
                "vec_rowid INTEGER NOT NULL UNIQUE, created INTEGER NOT NULL,"
                "PRIMARY KEY(hash, model))"
            )
            if not self._has_fts():
                self._create_fts()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (SCHEMA_VERSION,)
            )

    def _has_fts(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts'"
        ).fetchone()
        return row is not None

    def _create_fts(self) -> None:
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE fts USING fts5("
                "uri UNINDEXED, title, abstract, overview, body, keywords, tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            self._trigram = False
            logger.warning("SQLite 不支持 trigram 分词，中文子串检索可能退化")
            self._conn.execute(
                "CREATE VIRTUAL TABLE fts USING fts5("
                "uri UNINDEXED, title, abstract, overview, body, keywords, tokenize='unicode61')"
            )

    def is_empty(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone()
        return row is None

    def upsert(self, node: Node, body: Optional[str] = None, text_hash: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO nodes(uri, user, agent, kind, title, title_key, abstract, overview,"
                " tags, keywords, created, updated, importance, text_hash)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uri) DO UPDATE SET title=excluded.title, title_key=excluded.title_key,"
                " abstract=excluded.abstract, overview=excluded.overview, tags=excluded.tags,"
                " keywords=excluded.keywords, created=excluded.created, updated=excluded.updated,"
                " importance=excluded.importance, text_hash=excluded.text_hash",
                (
                    node.uri,
                    _owner(node.uri)[0],
                    _owner(node.uri)[1],
                    node.kind,
                    node.title,
                    node.title_key,
                    node.abstract,
                    node.overview,
                    json.dumps(list(node.tags), ensure_ascii=False),
                    json.dumps(list(node.keywords), ensure_ascii=False),
                    node.created,
                    node.updated,
                    node.importance,
                    text_hash,
                ),
            )
            self._conn.execute("DELETE FROM fts WHERE uri=?", (node.uri,))
            self._conn.execute(
                "INSERT INTO fts(uri, title, abstract, overview, body, keywords) VALUES(?,?,?,?,?,?)",
                (
                    node.uri,
                    node.title,
                    node.abstract,
                    node.overview,
                    body if body is not None else node.body,
                    " ".join(node.keywords),
                ),
            )

    def remove(self, uri: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM nodes WHERE uri=?", (uri,))
            self._conn.execute("DELETE FROM fts WHERE uri=?", (uri,))

    def reindex(self, tagged_nodes: Iterable[tuple[Node, str]]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM nodes")
            self._conn.execute("DELETE FROM fts")
        for node, text_hash in tagged_nodes:
            try:
                self.upsert(node, text_hash=text_hash)
            except sqlite3.Error as exc:
                logger.warning("记忆索引写入失败（%s）：%s", node.uri, type(exc).__name__)

    def search(
        self, user: str, agent: str, text: str, *, scope: str = "all", limit: int = 5,
    ) -> list[Hit]:
        kinds = SCOPE_KINDS.get(scope, SCOPE_KINDS["all"])
        placeholders = ",".join("?" for _ in kinds)
        params: list = [user, agent, *kinds]
        match = _match(text)
        with self._lock:
            if match is not None:
                sql = (
                    "SELECT n.uri, n.kind, n.title, n.abstract, n.tags, n.updated, bm25(fts) AS score"
                    " FROM fts JOIN nodes n ON n.uri=fts.uri"
                    f" WHERE fts MATCH ? AND n.user=? AND n.agent=? AND n.kind IN ({placeholders})"
                    " ORDER BY score, n.updated DESC LIMIT ?"
                )
                rows = self._conn.execute(sql, [match, *params, limit]).fetchall()
            else:
                terms = [item for item in re.split(r"\s+", text.strip()) if item]
                if not terms:
                    return []
                groups = []
                params = [user, agent, *kinds]
                for term in terms:
                    like = _like(term)
                    groups.append(
                        "(f.title LIKE ? ESCAPE '\\' OR f.abstract LIKE ? ESCAPE '\\'"
                        " OR f.overview LIKE ? ESCAPE '\\' OR f.body LIKE ? ESCAPE '\\'"
                        " OR f.keywords LIKE ? ESCAPE '\\')"
                    )
                    params.extend([like, like, like, like, like])
                sql = (
                    "SELECT n.uri, n.kind, n.title, n.abstract, n.tags, n.updated, 0.0 AS score"
                    " FROM fts f JOIN nodes n ON n.uri=f.uri"
                    f" WHERE n.user=? AND n.agent=? AND n.kind IN ({placeholders})"
                    " AND " + " AND ".join(groups) +
                    " ORDER BY n.updated DESC LIMIT ?"
                )
                rows = self._conn.execute(sql, [*params, limit]).fetchall()
        return [
            Hit(
                uri=row["uri"],
                kind=row["kind"],
                title=row["title"],
                abstract=row["abstract"],
                tags=tuple(json.loads(row["tags"] or "[]")),
                updated=int(row["updated"]),
                score=float(row["score"] or 0.0),
            )
            for row in rows
        ]

    def find_title(self, user: str, agent: str, title_key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT uri FROM nodes WHERE user=? AND agent=? AND title_key=? LIMIT 1",
                (user, agent, title_key),
            ).fetchone()
        return str(row["uri"]) if row is not None else None

    def page(self, user: str, agent: str, *, limit: int, page: int) -> Tuple[list[Hit], int]:
        offset = max(page - 1, 0) * limit
        kinds = LIST_KINDS
        placeholders = ",".join("?" for _ in kinds)
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM nodes WHERE user=? AND agent=? AND kind IN ("
                + placeholders + ")",
                (user, agent, *kinds),
            ).fetchone()["n"]
            rows = self._conn.execute(
                "SELECT uri, kind, title, abstract, tags, updated, 0.0 AS score FROM nodes"
                " WHERE user=? AND agent=? AND kind IN (" + placeholders + ")"
                " ORDER BY updated DESC, uri LIMIT ? OFFSET ?",
                (user, agent, *kinds, limit, offset),
            ).fetchall()
        return (
            [
                Hit(
                    uri=row["uri"],
                    kind=row["kind"],
                    title=row["title"],
                    abstract=row["abstract"],
                    tags=tuple(json.loads(row["tags"] or "[]")),
                    updated=int(row["updated"]),
                    score=0.0,
                )
                for row in rows
            ],
            int(total),
        )

    def counts(self, user: str, agent: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM nodes WHERE user=? AND agent=? GROUP BY kind",
                (user, agent),
            ).fetchall()
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def _load_vec(self) -> bool:
        """加载 sqlite-vec 扩展；失败只记一次，此后语义路不可用。"""
        if self._vec_loaded is not None:
            return self._vec_loaded
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            self._conn.load_extension(sqlite_vec.loadable_path())
            self._vec_loaded = True
        except Exception as exc:
            logger.warning("sqlite-vec 扩展不可用（%s）：%s", type(exc).__name__, exc)
            self._vec_loaded = False
        return self._vec_loaded

    def ensure_vector_table(self, dim: int) -> bool:
        """确保 vec0 虚表存在且维度匹配；维度变化或扩展不可用时返回 False。"""
        if not self._load_vec():
            return False
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='vec_dim'"
            ).fetchone()
            table = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_items'"
            ).fetchone()
            if row is not None and table is not None and int(row["value"]) == dim:
                return True
            self._conn.execute("DROP TABLE IF EXISTS vec_items")
            self._conn.execute(
                f"CREATE VIRTUAL TABLE vec_items USING vec0("
                f"user TEXT PARTITION KEY, agent TEXT, kind TEXT, model TEXT,"
                f" embedding float[{dim}])"
            )
            self._conn.execute("DELETE FROM embeddings_meta")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('vec_dim', ?)", (str(dim),)
            )
        return True

    def set_embedding(
        self, text_hash: str, model: str, dim: int, vector: Sequence[float],
        *, user: str, agent: str, kind: str,
    ) -> None:
        rowid = _vec_rowid(text_hash)
        blob = struct.pack(f"{len(vector)}f", *vector)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM vec_items WHERE rowid=?", (rowid,))
            self._conn.execute(
                "INSERT INTO vec_items(rowid, user, agent, kind, model, embedding)"
                " VALUES(?,?,?,?,?,?)",
                (rowid, user, agent, kind, model, blob),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO embeddings_meta"
                "(hash, model, dim, user, agent, kind, vec_rowid, created)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (text_hash, model, dim, user, agent, kind, rowid, int(time.time())),
            )

    def drop_embedding(self, text_hash: str, model: str) -> None:
        rowid = _vec_rowid(text_hash)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM vec_items WHERE rowid=?", (rowid,))
            self._conn.execute(
                "DELETE FROM embeddings_meta WHERE hash=? AND model=?", (text_hash, model)
            )

    def has_embedding(self, text_hash: str, model: str) -> bool:
        if not text_hash:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM embeddings_meta WHERE hash=? AND model=? LIMIT 1",
                (text_hash, model),
            ).fetchone()
        return row is not None

    def missing_hashes(self, text_hashes: Sequence[str], model: str) -> set[str]:
        """返回这些文本哈希里当前模型还没有向量的部分。"""
        missing = {h for h in text_hashes if h}
        hashes = list(missing)
        for start in range(0, len(hashes), 200):
            chunk = hashes[start:start + 200]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT hash FROM embeddings_meta WHERE model=? AND hash IN ({placeholders})",
                    (model, *chunk),
                ).fetchall()
            for row in rows:
                missing.discard(str(row["hash"]))
        return missing

    def semantic_search(
        self, user: str, agent: str, kinds: Sequence[str], model: str, dim: int,
        query_vector: Sequence[float], *, limit: int,
    ) -> list[Hit]:
        """vec0 KNN：元数据过滤在 C 层完成；返回按余弦降序的候选。"""
        if not self._load_vec():
            return []
        kinds_marks = ",".join("?" for _ in kinds)
        query_blob = struct.pack(f"{len(query_vector)}f", *query_vector)
        with self._lock:
            knn = self._conn.execute(
                "SELECT rowid, distance FROM vec_items"
                " WHERE embedding MATCH ? AND k = ?"
                f" AND user = ? AND agent = ? AND model = ? AND kind IN ({kinds_marks})",
                (query_blob, limit, user, agent, model, *kinds),
            ).fetchall()
            if not knn:
                return []
            rowids = [int(row["rowid"]) for row in knn]
            rowid_marks = ",".join("?" for _ in rowids)
            meta_rows = self._conn.execute(
                "SELECT vec_rowid, hash FROM embeddings_meta"
                f" WHERE model=? AND vec_rowid IN ({rowid_marks})",
                (model, *rowids),
            ).fetchall()
        distance_by_rowid = {int(row["rowid"]): float(row["distance"]) for row in knn}
        hash_by_rowid = {int(row["vec_rowid"]): str(row["hash"]) for row in meta_rows}
        hashes = list(hash_by_rowid.values())
        if not hashes:
            return []
        hash_marks = ",".join("?" for _ in hashes)
        with self._lock:
            nodes = self._conn.execute(
                "SELECT uri, kind, title, abstract, tags, updated, text_hash FROM nodes"
                f" WHERE user=? AND agent=? AND kind IN ({kinds_marks}) AND text_hash IN ({hash_marks})"
                " ORDER BY updated DESC",
                (user, agent, *kinds, *hashes),
            ).fetchall()
        hits: dict[str, Hit] = {}
        for row in nodes:
            text_hash = str(row["text_hash"])
            rowid = next((rid for rid, h in hash_by_rowid.items() if h == text_hash), None)
            distance = distance_by_rowid.get(rowid) if rowid is not None else None
            if distance is None:
                continue
            score = 1.0 - distance * distance / 2.0  # 归一化向量：L2² = 2 - 2cos
            hits[str(row["uri"])] = Hit(
                uri=row["uri"],
                kind=row["kind"],
                title=row["title"],
                abstract=row["abstract"],
                tags=tuple(json.loads(row["tags"] or "[]")),
                updated=int(row["updated"]),
                score=score,
            )
        ordered = sorted(hits.values(), key=lambda hit: hit.score, reverse=True)
        return ordered[:limit]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def _owner(uri: str) -> Tuple[str, str]:
    parts = str(uri).split("/")
    return (parts[2], parts[3]) if len(parts) >= 5 else ("", "")
