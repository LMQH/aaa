"""文件树存储：URI、frontmatter、原子写与命名空间隔离。

文件树是唯一事实源；SQLite 只是派生索引。用户与 Agent 各自一个命名空间，
所有落盘路径都校验在命名空间根之下。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

import yaml

KINDS: Tuple[str, ...] = ("memories", "sessions", "resources", "skills")
KIND_SET = frozenset(KINDS)
_URI = re.compile(r"^viking://([^/]+)/([^/]+)/([^/]+)(?:/(.*))?$")
_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)


def slug(text: object, fallback: str = "node") -> str:
    """路径与标题键的稳定 slug；发生改写时追加哈希避免碰撞。"""
    raw = str(text or "").strip()
    cleaned = _UNSAFE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = fallback
    if cleaned != raw or len(cleaned) > 64:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[:56]}-{digest}"
    return cleaned


def clean_rel(rel: object) -> str:
    text = str(rel or "").replace("\\", "/").strip("/")
    if not text:
        return ""
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("路径不能包含 ..")
        parts.append(part)
    return "/".join(parts)


def parse_uri(uri: str) -> Tuple[str, str, str, str]:
    """拆分 `viking://user/agent/kind/rel`；kind 为空表示命名空间根。"""
    match = _URI.fullmatch(str(uri or "").strip())
    if match is None:
        raise ValueError("URI 不合法")
    user, agent, kind, rel = match.groups()
    if kind not in KIND_SET:
        raise ValueError(f"未知节点类型：{kind}")
    return user, agent, kind, clean_rel(rel or "")


@dataclass(frozen=True)
class Node:
    uri: str
    kind: str
    rel: str
    path: Path
    title: str
    abstract: str = ""
    overview: str = ""
    body: str = ""
    tags: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    created: int = 0
    updated: int = 0
    source: str = ""
    importance: float = 0.0
    title_key: str = ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _split(text: str) -> Tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except Exception:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), text[end + 5:]


def _strings(value: object) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


class Store:
    """vault 根；按身份分出命名空间。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def namespace(self, user_id: str, agent_id: str) -> "Namespace":
        return Namespace(self, slug(user_id, "user"), slug(agent_id, "agent"))

    def iter_nodes(self) -> Iterator[Node]:
        """遍历全部命名空间的节点，用于索引重建。"""
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root)
            if len(rel.parts) < 4:
                continue
            user, agent, kind = rel.parts[0], rel.parts[1], rel.parts[2]
            if kind not in KIND_SET:
                continue
            node_rel = "/".join(rel.parts[3:])
            try:
                node = self._read_path(path, user, agent, kind, node_rel)
            except Exception:
                continue
            if node is not None:
                yield node

    @staticmethod
    def _read_path(path: Path, user: str, agent: str, kind: str, rel: str) -> Optional[Node]:
        try:
            meta, body = _split(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        title = str(meta.get("title") or Path(rel).stem)
        return Node(
            uri=f"viking://{user}/{agent}/{kind}/{rel}",
            kind=kind,
            rel=rel,
            path=path,
            title=title,
            abstract=str(meta.get("abstract") or ""),
            overview=str(meta.get("overview") or ""),
            body=body,
            tags=_strings(meta.get("tags")),
            keywords=_strings(meta.get("keywords")),
            created=int(meta.get("created") or 0),
            updated=int(meta.get("updated") or 0),
            source=str(meta.get("source") or ""),
            importance=float(meta.get("importance") or 0.0),
            title_key=slug(title, "node"),
        )


class Namespace:
    def __init__(self, store: Store, user: str, agent: str) -> None:
        self.store = store
        self.user = user
        self.agent = agent

    @property
    def base(self) -> Path:
        return self.store.root / self.user / self.agent

    def uri(self, kind: str, rel: str = "") -> str:
        if kind not in KIND_SET:
            raise ValueError(f"未知节点类型：{kind}")
        cleaned = clean_rel(rel)
        suffix = f"/{cleaned}" if cleaned else ""
        return f"viking://{self.user}/{self.agent}/{kind}{suffix}"

    def parse(self, uri: str) -> Tuple[str, str]:
        """返回 `(kind, rel)`；只接受本命名空间的 URI。"""
        user, agent, kind, rel = parse_uri(uri)
        if user != self.user or agent != self.agent:
            raise ValueError("URI 不属于当前用户")
        return kind, rel

    def path(self, kind: str, rel: str = "") -> Path:
        if kind not in KIND_SET:
            raise ValueError(f"未知节点类型：{kind}")
        root = (self.base / kind).resolve()
        target = (root / clean_rel(rel)).resolve()
        if target != root and not target.is_relative_to(root):
            raise ValueError("路径越界")
        return target

    def write(
        self, kind: str, rel: str, *, title: str, abstract: str = "", overview: str = "",
        body: str = "", tags: Sequence[str] = (), keywords: Sequence[str] = (),
        source: str = "", importance: float = 0.0, created: Optional[int] = None,
    ) -> Node:
        cleaned = clean_rel(rel)
        if not cleaned:
            raise ValueError("缺少节点路径")
        now = int(time.time())
        stamp = int(created or now)
        meta = {
            "kind": kind,
            "title": str(title),
            "abstract": str(abstract),
            "overview": str(overview),
            "tags": [str(item) for item in tags],
            "keywords": [str(item) for item in keywords],
            "created": stamp,
            "updated": now,
            "source": str(source),
            "importance": float(importance),
        }
        text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + str(body)
        target = self.path(kind, cleaned)
        _atomic_write(target, text)
        return Node(
            uri=self.uri(kind, cleaned),
            kind=kind,
            rel=cleaned,
            path=target,
            title=str(title),
            abstract=str(abstract),
            overview=str(overview),
            body=str(body),
            tags=tuple(str(item) for item in tags),
            keywords=tuple(str(item) for item in keywords),
            created=stamp,
            updated=now,
            source=str(source),
            importance=float(importance),
            title_key=slug(title, "node"),
        )

    def read(self, uri: str) -> Optional[Node]:
        try:
            kind, rel = self.parse(uri)
        except ValueError:
            return None
        target = self.path(kind, rel)
        if not target.is_file():
            return None
        return Store._read_path(target, self.user, self.agent, kind, rel)

    def read_rel(self, kind: str, rel: str) -> Optional[Node]:
        return self.read(self.uri(kind, rel))

    def exists(self, uri: str) -> bool:
        try:
            kind, rel = self.parse(uri)
        except ValueError:
            return False
        return self.path(kind, rel).exists()

    def delete(self, uri: str) -> bool:
        kind, rel = self.parse(uri)
        target = self.path(kind, rel)
        if not target.is_file():
            return False
        target.unlink()
        return True

    def is_dir(self, kind: str, rel: str = "") -> bool:
        return self.path(kind, rel).is_dir()

    def entries(self, kind: str, rel: str = "") -> list[tuple[str, bool]]:
        """列出目录下的直接子项；文件返回自身。"""
        target = self.path(kind, rel)
        if target.is_file():
            return [(clean_rel(rel), False)]
        if not target.is_dir():
            return []
        base = target
        items = []
        for child in sorted(base.iterdir(), key=lambda item: item.name.lower()):
            if child.name.startswith("."):
                continue
            items.append((str(child.relative_to(self.path(kind))).replace(os.sep, "/"), child.is_dir()))
        return items

    def write_journal(self, rel: str, payload: dict) -> bool:
        """写入一次轮次原始记录；同一路径已存在则跳过（按 run 幂等）。"""
        cleaned = clean_rel(rel)
        if not cleaned:
            raise ValueError("缺少记录路径")
        target = (self.base / "journal" / cleaned).resolve()
        root = (self.base / "journal").resolve()
        if target != root and not target.is_relative_to(root):
            raise ValueError("路径越界")
        if target.exists():
            return False
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        _atomic_write(target, text)
        return True

    def read_journal(self, rel: str) -> Optional[dict]:
        cleaned = clean_rel(rel)
        target = (self.base / "journal" / cleaned).resolve()
        root = (self.base / "journal").resolve()
        if target != root and not target.is_relative_to(root):
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return None
