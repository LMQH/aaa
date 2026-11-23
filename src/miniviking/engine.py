"""MiniViking 引擎：文件树 + 派生索引 + LLM 蒸馏 + 持久任务。

宿主只通过 aaa 的外置记忆协议访问本类；身份由宿主传入，不从工具参数推断。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import tomllib
import uuid
from pathlib import Path
from typing import Optional, Sequence

from aaa.memory.protocol import MemoryEntry, MemoryIdentity, MemoryTool

from . import config as config_module
from .config import Config
from .distill import (
    dedupe_key,
    distill_turn,
    mechanical_summary,
    mechanical_turn,
    summarize_text,
)
from .embed import Embedder, build_embedder
from .index import SCOPE_KINDS, Hit, Index
from .llm import ChatClient, build_client
from .store import KINDS, Namespace, Node, Store, parse_uri, slug
from .tasks import Tasks
from .tools import TOOLS, dispatch

logger = logging.getLogger("aaa.memory.miniviking")

TEXT_EXT = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
    ".html", ".htm", ".css", ".sh", ".java", ".go", ".rs", ".c", ".h",
    ".cpp", ".sql", ".log",
})
_TASK_URI = re.compile(r"^viking://([^/]+)/([^/]+)/tasks/([A-Za-z0-9]+)$")
_NS_URI = re.compile(r"^viking://([^/]+)/([^/]+)/?$")

PROMPT_BLOCK = """MiniViking 维护一棵按用户隔离的上下文树，节点使用 viking:// URI：
- viking_search：按关键词检索记忆、资源与技能；结果带 URI。
- viking_read：读节点。abstract 是一句话摘要，overview 是概述，full 是正文；正文过长时用 offset/limit 分段。
- viking_browse：tree / list / stat 查看目录结构；根是 viking://<用户>/<agent>。
- viking_remember：写入值得长期保留的事实或决定。
- viking_forget：按精确 URI 删除节点。
- viking_add_resource：导入本地文件或目录，返回任务 URI；用 viking_read 查任务状态。
- viking_add_skill：写入一个技能说明（SKILL.md）；技能只是数据，不会被执行。
检索命中只是摘要；需要细节时用 viking_read 回读。不要用工具参数指定用户或会话身份。"""


def _render_toml(raw: dict) -> str:
    """把 section.field 两层结构渲染回 TOML；值只含标量。"""
    lines: list[str] = []
    for section, fields in raw.items():
        if not isinstance(fields, dict) or not fields:
            continue
        lines.append(f"\n[{section}]")
        for key, value in fields.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
                rendered = f'"{escaped}"'
            lines.append(f"{key} = {rendered}")
    return "\n".join(lines).lstrip("\n") + "\n"


class Engine:
    def __init__(
        self, data_dir: Path, *, client: Optional[ChatClient] = None,
        embedder: Optional[Embedder] = None, config: Optional[Config] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or config_module.load(self.data_dir)
        self.store = Store(self.data_dir / "vault")
        self.index = Index(self.data_dir / "state.sqlite3")
        if self.index.is_empty():
            self.index.reindex(self._tagged_nodes())
        self.client = client if client is not None else build_client(self.config.llm)
        if self.client is None:
            logger.warning("miniviking 未配置可用模型，蒸馏退化为机械摘要")
        self.embedder = embedder if embedder is not None else build_embedder(
            self.config.embedding, self.data_dir,
        )
        if self.embedder is not None:
            try:
                if not self.index.ensure_vector_table(self.embedder.dimension()):
                    raise RuntimeError("sqlite-vec 扩展不可用")
            except Exception as exc:
                logger.warning(
                    "miniviking 语义检索不可用（%s），已降级为关键词检索：%s",
                    type(exc).__name__, exc,
                )
                self.embedder = None
        if self.embedder is not None:
            logger.info("miniviking 语义检索已启用（%s）", self.embedder.name())
        self._dirty: set[str] = set()
        self._query_cache: dict[tuple[str, str], list[float]] = {}
        self.tasks = Tasks(
            self.data_dir / "state.sqlite3", self._run_task,
            close_timeout=self.config.limits.close_timeout,
        )
        if self.embedder is not None:
            self.tasks.enqueue("embed", {"uris": []})
        self._closed = False

    def _tagged_nodes(self):
        max_chars = self.config.embedding.max_chars
        for node in self.store.iter_nodes():
            yield node, _text_hash(_embed_text(node, max_chars))

    # ---- aaa 协议 -------------------------------------------------------

    def recall(self, query) -> Sequence[MemoryEntry]:
        text = str(getattr(query, "text", "") or "").strip()
        if not text:
            return ()
        ns = self._namespace(query.identity)
        hits = self._hybrid(
            ns.user, ns.agent, text, scope="memory", limit=max(int(query.limit), 1),
        )
        return tuple(self._entry(hit) for hit in hits)

    def observe(self, observation) -> None:
        ns = self._namespace(observation.identity)
        session = str(observation.identity.session_id or "")
        run = str(observation.identity.run_id or "")
        rel = f"{slug(session or 'none', 'session')}/{slug(run or _fresh_run(), 'run')}.json"
        payload = {
            "query": str(observation.query or ""),
            "answer": str(observation.answer or ""),
            "user_id": observation.identity.user_id,
            "agent_id": observation.identity.agent_id,
            "session_id": session,
            "run_id": run,
            "created": int(time.time()),
        }
        if not ns.write_journal(rel, payload):
            return
        self.tasks.enqueue("distill", {
            "user": ns.user,
            "agent": ns.agent,
            "journal": rel,
        })
        logger.info("miniviking 已记录一轮（user=%s session=%s）", ns.user, session or "-")

    def read(self, identity: MemoryIdentity, *, limit: int, page: int):
        ns = self._namespace(identity)
        size = max(1, min(int(limit), self.config.limits.list_limit))
        hits, total = self.index.page(ns.user, ns.agent, limit=size, page=max(int(page), 1))
        return tuple(self._entry(hit) for hit in hits), total

    def tools(self) -> Sequence[MemoryTool]:
        return TOOLS

    def call_tool(self, name: str, arguments: Optional[dict], identity: MemoryIdentity) -> str:
        return dispatch(self, name, dict(arguments or {}), identity)

    def prompt_block(self) -> str:
        return PROMPT_BLOCK

    def config_specs(self) -> list[dict]:
        """声明可调配置项；宿主据此渲染 `/memory config` 编辑面并校验输入。"""
        c = self.config
        specs: list[dict] = []

        def add(section: str, key: str, kind: str, current, description: str,
                *, minimum=None, maximum=None, secret: bool = False, choices=None):
            specs.append({
                "key": f"{section}.{key}", "section": section, "type": kind,
                "current": current, "minimum": minimum, "maximum": maximum,
                "choices": list(choices) if choices else None,
                "secret": secret, "description": description,
            })

        add("llm", "inherit", "bool", c.llm.inherit, "蒸馏模型继承 aaa 默认渠道；false 时需显式 provider/model")
        add("llm", "provider", "str", c.llm.provider, "独立蒸馏渠道 id（inherit=false 时生效）")
        add("llm", "model", "str", c.llm.model, "独立蒸馏模型 id")
        add("llm", "base_url", "str", c.llm.base_url, "蒸馏端点（OpenAI 兼容）")
        add("llm", "api_key", "str", c.llm.api_key, "蒸馏端点密钥（落引擎配置文件）", secret=True)
        add("llm", "api_key_env", "str", c.llm.api_key_env, "从环境变量读密钥的名字")
        add("llm", "temperature", "float", c.llm.temperature, "蒸馏采样温度", minimum=0.0, maximum=2.0)
        add("llm", "max_tokens", "int", c.llm.max_tokens, "蒸馏单次输出 token 上限", minimum=64, maximum=32_000)
        add("llm", "timeout", "float", c.llm.timeout, "蒸馏调用超时（秒）", minimum=5.0, maximum=600.0)
        add("limits", "max_memories_per_turn", "int", c.limits.max_memories_per_turn, "每轮最多抽取的记忆条数", minimum=1, maximum=20)
        add("limits", "max_turn_chars", "int", c.limits.max_turn_chars, "送蒸馏的单轮字符上限", minimum=500, maximum=60_000)
        add("limits", "max_file_bytes", "int", c.limits.max_file_bytes, "单个导入文件字节上限", minimum=1_024, maximum=64 * 1024 * 1024)
        add("limits", "max_files_per_import", "int", c.limits.max_files_per_import, "单次导入文件数上限", minimum=1, maximum=5_000)
        add("limits", "max_import_depth", "int", c.limits.max_import_depth, "导入目录递归深度", minimum=1, maximum=10)
        add("limits", "chunk_chars", "int", c.limits.chunk_chars, "资源切块字符数", minimum=500, maximum=20_000)
        add("limits", "max_chunks_per_resource", "int", c.limits.max_chunks_per_resource, "单资源最大块数", minimum=1, maximum=200)
        add("limits", "search_limit", "int", c.limits.search_limit, "检索候选上限", minimum=1, maximum=200)
        add("limits", "read_full_chars", "int", c.limits.read_full_chars, "整读输出的字符上限", minimum=500, maximum=40_000)
        add("limits", "browse_depth", "int", c.limits.browse_depth, "浏览默认递归深度", minimum=1, maximum=8)
        add("limits", "list_limit", "int", c.limits.list_limit, "列出节点的条数上限", minimum=1, maximum=1_000)
        add("limits", "close_timeout", "float", c.limits.close_timeout, "close 等待上限（秒）", minimum=0.5, maximum=4.0)
        add("embedding", "enable", "bool", c.embedding.enable, "启用语义向量检索；关闭则退化为关键词")
        add("embedding", "provider", "str", c.embedding.provider, "local（GGUF 本地推理）或 api（OpenAI 兼容）", choices=("local", "api"))
        add("embedding", "model", "str", c.embedding.model, "BGE 模型名（GGUF 仓库名随之推导）")
        add("embedding", "endpoint", "str", c.embedding.endpoint, "权重下载镜像；置空直连 huggingface.co")
        add("embedding", "model_path", "str", c.embedding.model_path, "本地 GGUF 路径；设置后跳过下载")
        add("embedding", "repo", "str", c.embedding.repo, "GGUF 所在 HF 仓库（默认 gpustack/{model}-GGUF）")
        add("embedding", "file", "str", c.embedding.file, "GGUF 文件名（默认 {model}-f16.gguf）")
        add("embedding", "base_url", "str", c.embedding.base_url, "provider=api 时的端点")
        add("embedding", "api_key", "str", c.embedding.api_key, "embedding 端点密钥（落引擎配置文件）", secret=True)
        add("embedding", "api_key_env", "str", c.embedding.api_key_env, "从环境变量读密钥的名字")
        add("embedding", "dimensions", "int", c.embedding.dimensions, "向量维度；0 表示从模型自动探测", minimum=0, maximum=4096)
        add("embedding", "query_instruction", "str", c.embedding.query_instruction, "只加在查询前的前缀指令")
        add("embedding", "max_chars", "int", c.embedding.max_chars, "单条节点参与嵌入的字符上限", minimum=100, maximum=8_000)
        add("embedding", "batch", "int", c.embedding.batch, "嵌入批量", minimum=1, maximum=128)
        return specs

    def apply_config(self, values: dict) -> None:
        """把键值合并进 <data_dir>/config.toml；引擎在下次启动时读取。"""
        path = self.data_dir / "config.toml"
        raw: dict = {}
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"现有配置无法解析，请手工修正 {path}：{type(exc).__name__}") from exc
        if not isinstance(raw, dict):
            raw = {}
        for key, value in values.items():
            section, _, field = str(key).partition(".")
            if not field:
                raise ValueError(f"配置键须为 section.field：{key}")
            raw.setdefault(section, {})[field] = value
        path.write_text(_render_toml(raw), encoding="utf-8")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.tasks.close()
        self.index.close()

    # ---- 工具实现 -------------------------------------------------------

    def search(self, identity: MemoryIdentity, text: str, *, scope: str, limit: int) -> list[Hit]:
        ns = self._namespace(identity)
        cap = max(1, min(int(limit), self.config.limits.search_limit))
        return self._hybrid(ns.user, ns.agent, text, scope=scope, limit=cap)

    def _hybrid(self, user: str, agent: str, text: str, *, scope: str, limit: int) -> list[Hit]:
        hits = self.index.search(user, agent, text, scope=scope, limit=limit)
        if self.embedder is None or not text.strip():
            return hits
        query_vector = self._query_vector(text)
        if query_vector is None:
            return hits
        kinds = SCOPE_KINDS.get(scope, SCOPE_KINDS["all"])
        semantic = self.index.semantic_search(
            user, agent, kinds, self.embedder.name(), self.embedder.dimension(),
            query_vector, limit=limit,
        )
        return _rrf_merge(hits, semantic, limit)

    def _query_vector(self, text: str) -> Optional[list[float]]:
        key = (self.embedder.name(), text)
        cached = self._query_cache.get(key)
        if cached is not None:
            return cached
        try:
            prompt = f"{self.config.embedding.query_instruction}{text}"
            vector = self.embedder.embed([prompt])[0]
        except Exception as exc:
            logger.warning("查询嵌入失败，本轮退化为关键词检索：%s", type(exc).__name__)
            return None
        if len(self._query_cache) > 64:
            self._query_cache.clear()
        self._query_cache[key] = vector
        return vector

    def read_target(
        self, identity: MemoryIdentity, target: str, *, level: str, offset: int, limit: int,
    ) -> str:
        ns = self._namespace(identity)
        task_id = _task_id(target)
        if task_id is not None:
            return self._task_text(ns, task_id)
        try:
            kind, _rel = ns.parse(target)
        except ValueError as exc:
            return f"Error: {exc}"
        node = ns.read(target)
        if node is None:
            return f"Error: 未找到 {target}"
        return self._render_node(node, level, offset, limit)

    def browse(self, identity: MemoryIdentity, target: str, *, action: str, depth: int) -> str:
        ns = self._namespace(identity)
        task_id = _task_id(target)
        if task_id is not None:
            return self._task_text(ns, task_id)
        if not str(target or "").strip():
            return self._root_listing(ns)
        if _NS_URI.fullmatch(str(target).strip()):
            match = _NS_URI.fullmatch(str(target).strip())
            if match is not None and (match.group(1), match.group(2)) != (ns.user, ns.agent):
                return "Error: URI 不属于当前用户"
            return self._root_listing(ns)
        try:
            kind, rel = ns.parse(target)
        except ValueError as exc:
            return f"Error: {exc}"
        if action == "stat":
            node = ns.read(target)
            if node is not None:
                return self._stat(node)
            if ns.path(kind, rel).is_dir():
                return f"{target}\n目录 · {len(ns.entries(kind, rel))} 项"
            return f"Error: 未找到 {target}"
        if action == "tree":
            return self._tree(ns, kind, rel, max(1, min(depth, self.config.limits.browse_depth)))
        return self._listing(ns, kind, rel)

    def remember(
        self, identity: MemoryIdentity, *, title: str, content: str,
        tags: Sequence[str] = (), keywords: Sequence[str] = (), importance: float = 0.5,
    ) -> str:
        title = str(title or "").strip()
        content = str(content or "").strip()
        if not title or not content:
            return "Error: 标题和内容都不能为空"
        node = self._write_memory_node(self._namespace(identity), {
            "title": title,
            "abstract": content[:240],
            "overview": content[:1200],
            "body": content,
            "tags": [str(item) for item in tags],
            "keywords": [str(item) for item in keywords] or _keywords(content),
            "importance": importance,
        })
        self._flush_embed()
        return f"已写入 {node.uri}"

    def forget(self, identity: MemoryIdentity, target: str) -> str:
        ns = self._namespace(identity)
        try:
            ns.parse(target)
        except ValueError as exc:
            return f"Error: {exc}"
        if not ns.exists(target):
            return f"Error: 未找到 {target}"
        if ns.read(target) is None:
            return "Error: 只能删除节点文件，不能删除目录"
        node = ns.read(target)
        ns.delete(target)
        self.index.remove(target)
        if self.embedder is not None:
            self.index.drop_embedding(
                _text_hash(_embed_text(node, self.config.embedding.max_chars)),
                self.embedder.name(),
            )
        return f"已删除 {target}"

    def add_resource(
        self, identity: MemoryIdentity, *, source: str, kind: str = "auto", tags: Sequence[str] = (),
    ) -> str:
        path = Path(str(source or "")).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path)
        try:
            path = path.resolve()
        except OSError:
            return f"Error: 路径无法解析：{source}"
        if not path.exists():
            return f"Error: 不存在：{path}"
        resolved_kind = "dir" if path.is_dir() else "file"
        if kind not in ("auto", "file", "dir"):
            return "Error: kind 只能是 auto、file 或 dir"
        if kind != "auto" and kind != resolved_kind:
            return f"Error: {path} 不是 {kind}"
        ns = self._namespace(identity)
        dest = slug(path.stem or path.name, "resource")
        task_id = self.tasks.enqueue("resource", {
            "user": ns.user,
            "agent": ns.agent,
            "source": str(path),
            "kind": resolved_kind,
            "dest": dest,
            "tags": [str(item) for item in tags],
        })
        return f"已排队导入：{ns.uri('resources', dest)} · 任务 {self._task_uri(ns, task_id)}"

    def add_skill(
        self, identity: MemoryIdentity, *, name: str, content: str, overwrite: bool = False,
    ) -> str:
        name = str(name or "").strip()
        content = str(content or "").strip()
        if not name or not content:
            return "Error: 技能名和内容都不能为空"
        ns = self._namespace(identity)
        rel = f"{slug(name, 'skill')}/SKILL.md"
        existing = ns.read_rel("skills", rel)
        if existing is not None and not overwrite:
            return f"Error: 技能已存在：{existing.uri}（需要覆盖请传 overwrite=true）"
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        title = lines[0].lstrip("#").strip() if lines else name
        node = ns.write(
            "skills", rel, title=title or name,
            abstract=content[:240], overview=content[:1200], body=content,
            tags=("skill",), keywords=_keywords(content),
            created=existing.created if existing else None,
        )
        self._save_node(node)
        self._flush_embed()
        return f"已写入技能 {node.uri}"

    # ---- 后台任务 -------------------------------------------------------

    def _run_task(self, task: dict) -> str:
        payload = json.loads(task["payload"])
        if task["kind"] == "distill":
            return self._distill(payload)
        if task["kind"] == "resource":
            return self._import_resource(payload)
        if task["kind"] == "embed":
            return self._embed_task(payload)
        raise ValueError(f"未知任务类型：{task['kind']}")

    def _distill(self, payload: dict) -> str:
        ns = self.store.namespace(payload["user"], payload["agent"])
        journal = ns.read_journal(payload["journal"])
        if journal is None:
            raise ValueError("原始轮次记录不存在")
        session = str(journal.get("session_id") or "")
        previous = ""
        session_rel = f"{slug(session or 'none', 'session')}.md"
        existing_session = ns.read_rel("sessions", session_rel)
        if existing_session is not None:
            previous = existing_session.body
        query = str(journal.get("query") or "")
        answer = str(journal.get("answer") or "")
        if self.client is not None:
            result = distill_turn(
                self.client, query=query, answer=answer, previous_summary=previous,
                limit=self.config.limits.max_memories_per_turn,
                max_chars=self.config.limits.max_turn_chars,
            )
        else:
            result = mechanical_turn(query, answer, self.config.limits.max_memories_per_turn)
        written = 0
        for item in result["memories"]:
            self._write_memory_node(ns, item)
            written += 1
        summary = result["session"]["summary"]
        if summary:
            node = ns.write(
                "sessions", session_rel,
                title="会话摘要",
                abstract=summary[:240],
                overview=summary[:1200],
                body=summary,
                tags=result["session"]["topics"],
                keywords=result["session"]["keywords"] or _keywords(summary),
                created=existing_session.created if existing_session else None,
            )
            self._save_node(node)
        self._flush_embed()
        return f"提取 {written} 条记忆"

    def _import_resource(self, payload: dict) -> str:
        ns = self.store.namespace(payload["user"], payload["agent"])
        source = Path(str(payload["source"]))
        dest = str(payload["dest"])
        tags = tuple(payload.get("tags") or ())
        if payload["kind"] == "file":
            files = [source]
            skipped: list[str] = []
        else:
            files, skipped = self._collect_files(source)
        done = 0
        failures: list[str] = []
        titles: list[str] = []
        for index, path in enumerate(files):
            try:
                if payload["kind"] == "file":
                    root_rel = dest
                    title = path.name
                else:
                    root_rel = f"{dest}/files/{index + 1:03d}-{slug(path.stem, 'file')}"
                    title = path.name
                title, _abstract, _overview = self._ingest_file(ns, path, root_rel, title, tags)
                titles.append(title)
                done += 1
            except Exception as exc:
                failures.append(f"{path.name}: {type(exc).__name__}")
        if payload["kind"] == "dir":
            overview = "\n".join(f"- {item}" for item in titles)[:1200]
            node = ns.write(
                "resources", f"{dest}/_index.md",
                title=source.name, abstract=f"导入 {done} 个文件，跳过 {len(skipped)} 个",
                overview=overview, body="", tags=tags,
                keywords=_keywords(" ".join(titles)),
            )
            self._save_node(node)
        message = f"导入 {done} 个文件"
        if skipped:
            message += f"，跳过 {len(skipped)} 个"
        if failures:
            message += f"，失败 {len(failures)} 个（{'; '.join(failures[:3])}）"
        self._flush_embed()
        return message

    def _ingest_file(
        self, ns: Namespace, path: Path, root_rel: str, title: str, tags: Sequence[str],
    ) -> tuple[str, str, str]:
        limits = self.config.limits
        if path.stat().st_size > limits.max_file_bytes:
            raise ValueError("文件超过大小上限")
        if path.suffix.lower() not in TEXT_EXT:
            raise ValueError("不支持的文件类型")
        text = path.read_text(encoding="utf-8", errors="replace")
        if "\x00" in text:
            raise ValueError("二进制文件")
        chunks = _split_text(text, limits.chunk_chars)[: limits.max_chunks_per_resource]
        if not chunks:
            raise ValueError("文件没有可索引文本")
        abstracts: list[str] = []
        overviews: list[str] = []
        for number, chunk in enumerate(chunks, 1):
            if self.client is not None:
                try:
                    summary = summarize_text(self.client, chunk, limits.chunk_chars)
                except Exception as exc:
                    logger.warning("miniviking 片段摘要失败：%s", type(exc).__name__)
                    summary = mechanical_summary(chunk)
            else:
                summary = mechanical_summary(chunk)
            node = ns.write(
                "resources", f"{root_rel}/chunk-{number:03d}.md",
                title=f"{title} 第 {number} 段",
                abstract=summary["abstract"],
                overview=summary["overview"],
                body=chunk,
                tags=tuple(dict.fromkeys([*tags, *summary["tags"]])),
                keywords=summary["keywords"] or _keywords(chunk),
                source=str(path),
            )
            self._save_node(node)
            abstracts.append(summary["abstract"])
            overviews.append(summary["overview"])
        abstract = abstracts[0] if abstracts else ""
        overview = "\n\n".join(overviews)[:1200]
        node = ns.write(
            "resources", f"{root_rel}/_index.md",
            title=title, abstract=abstract, overview=overview, body="",
            tags=tags, keywords=_keywords(title + " " + " ".join(abstracts)),
            source=str(path),
        )
        self._save_node(node)
        return title, abstract, overview

    def _collect_files(self, root: Path) -> tuple[list[Path], list[str]]:
        limits = self.config.limits
        files: list[Path] = []
        skipped: list[str] = []
        for current, dirs, names in os.walk(root):
            depth = len(Path(current).relative_to(root).parts)
            if depth >= limits.max_import_depth:
                dirs[:] = []
            for name in sorted(names):
                path = Path(current) / name
                if len(files) >= limits.max_files_per_import:
                    skipped.append(f"达到文件数上限 {limits.max_files_per_import}")
                    return files, skipped
                if path.suffix.lower() not in TEXT_EXT:
                    skipped.append(f"{name}: 类型不支持")
                    continue
                try:
                    if path.stat().st_size > limits.max_file_bytes:
                        skipped.append(f"{name}: 超过大小上限")
                        continue
                except OSError:
                    skipped.append(f"{name}: 无法读取")
                    continue
                files.append(path)
        return files, skipped

    # ---- 内部工具 -------------------------------------------------------

    def _save_node(self, node: Node) -> str:
        """写入索引并登记待嵌入；返回该节点当前的文本哈希。"""
        text = _embed_text(node, self.config.embedding.max_chars)
        text_hash = _text_hash(text)
        self.index.upsert(node, text_hash=text_hash)
        if self.embedder is not None:
            self._dirty.add(node.uri)
        return text_hash

    def _flush_embed(self) -> None:
        if self.embedder is None or not self._dirty:
            return
        uris = sorted(self._dirty)
        self._dirty.clear()
        self.tasks.enqueue("embed", {"uris": uris})

    def _embed_task(self, payload: dict) -> str:
        if self.embedder is None:
            raise ValueError("词嵌入未启用")
        uris = list(payload.get("uris") or [])
        entries = []  # (node, user, agent, kind)
        if uris:
            for uri in uris:
                try:
                    user, agent, kind, _rel = parse_uri(uri)
                except ValueError:
                    continue
                node = self.store.namespace(user, agent).read(uri)
                if node is not None:
                    entries.append((node, user, agent, kind))
        else:
            for node in self.store.iter_nodes():
                parts = node.uri.split("/")
                if len(parts) >= 4:
                    entries.append((node, parts[2], parts[3], node.kind))
        model = self.embedder.name()
        dim = self.embedder.dimension()
        max_chars = self.config.embedding.max_chars
        batch = self.config.embedding.batch
        pending = []
        for node, user, agent, kind in entries:
            text = _embed_text(node, max_chars)
            text_hash = _text_hash(text)
            if not self.index.has_embedding(text_hash, model):
                pending.append((text, text_hash, user, agent, kind))
        if not pending:
            return "无需新增向量"
        done = 0
        for start in range(0, len(pending), max(batch, 1)):
            chunk = pending[start:start + max(batch, 1)]
            vectors = self.embedder.embed([text for text, *_rest in chunk])
            for (text, text_hash, user, agent, kind), vector in zip(chunk, vectors):
                self.index.set_embedding(
                    text_hash, model, dim, vector, user=user, agent=agent, kind=kind,
                )
                done += 1
        return f"嵌入 {done} 个节点"

    def _write_memory_node(self, ns: Namespace, item: dict) -> Node:
        title = str(item.get("title") or "记忆").strip()
        body = str(item.get("body") or "").strip()
        if not body:
            body = str(item.get("overview") or item.get("abstract") or "").strip()
        title_key = slug(title, "memory")
        existing_uri = self.index.find_title(ns.user, ns.agent, title_key)
        existing = ns.read(existing_uri) if existing_uri else None
        tags = tuple(dict.fromkeys([*(existing.tags if existing else ()), *item.get("tags", ())]))
        keywords = tuple(dict.fromkeys(
            [*(existing.keywords if existing else ()), *item.get("keywords", ())]
        ))
        if existing is not None:
            node = ns.write(
                "memories", existing.rel,
                title=title, abstract=str(item.get("abstract") or existing.abstract),
                overview=str(item.get("overview") or existing.overview),
                body=body or existing.body, tags=tags, keywords=keywords,
                importance=max(existing.importance, float(item.get("importance") or 0.0)),
                created=existing.created,
            )
        else:
            rel = f"{time.strftime('%Y-%m-%d')}-{title_key}-{dedupe_key(title, body)}.md"
            node = ns.write(
                "memories", rel,
                title=title, abstract=str(item.get("abstract") or ""),
                overview=str(item.get("overview") or ""), body=body,
                tags=tags, keywords=keywords,
                importance=float(item.get("importance") or 0.0),
            )
        self._save_node(node)
        return node

    def _namespace(self, identity: MemoryIdentity) -> Namespace:
        return self.store.namespace(identity.user_id, identity.agent_id)

    def _entry(self, hit: Hit) -> MemoryEntry:
        text = f"[{hit.uri}] {hit.title}"
        if hit.abstract:
            text += f" — {hit.abstract}"
        return MemoryEntry(
            memory=text,
            memory_id=hit.uri,
            topics=hit.tags,
            updated_at=hit.updated or None,
            score=hit.score,
        )

    def _render_node(self, node: Node, level: str, offset: int, limit: int) -> str:
        if level == "abstract":
            body = node.abstract or node.overview or node.body
        elif level == "overview":
            body = node.overview or node.abstract or node.body
        else:
            body = node.body or node.overview or node.abstract
            cap = self.config.limits.read_full_chars
            start = max(int(offset), 0)
            length = max(1, min(int(limit or cap), cap))
            body = body[start:start + length]
        header = f"{node.uri}\n{node.title}"
        if node.kind == "memories" and node.importance:
            header += f" · 重要度 {node.importance:.1f}"
        if node.tags:
            header += f" · {'、'.join(node.tags)}"
        return f"{header}\n\n{body or '（空）'}"

    def _root_listing(self, ns: Namespace) -> str:
        counts = self.index.counts(ns.user, ns.agent)
        lines = [f"viking://{ns.user}/{ns.agent}"]
        for kind in KINDS:
            lines.append(f"- {kind}: {counts.get(kind, 0)} 条")
        return "\n".join(lines)

    def _listing(self, ns: Namespace, kind: str, rel: str) -> str:
        entries = ns.entries(kind, rel)
        if not entries:
            return f"{ns.uri(kind, rel)}\n（空）"
        lines = [ns.uri(kind, rel)]
        for child, is_dir in entries[: self.config.limits.list_limit]:
            suffix = "/" if is_dir else ""
            lines.append(f"- {ns.uri(kind, child)}{suffix}")
        if len(entries) > self.config.limits.list_limit:
            lines.append(f"… 另有 {len(entries) - self.config.limits.list_limit} 项")
        return "\n".join(lines)

    def _tree(self, ns: Namespace, kind: str, rel: str, depth: int) -> str:
        lines = [ns.uri(kind, rel)]
        _walk(ns, kind, rel, depth, lines, level=0, limit=200)
        return "\n".join(lines)

    def _stat(self, node: Node) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(node.updated or 0))
        lines = [node.uri, node.title]
        if node.abstract:
            lines.append(node.abstract)
        if node.tags:
            lines.append(f"标签：{'、'.join(node.tags)}")
        if node.source:
            lines.append(f"来源：{node.source}")
        lines.append(f"更新：{when}")
        return "\n".join(lines)

    def _task_uri(self, ns: Namespace, task_id: str) -> str:
        return f"viking://{ns.user}/{ns.agent}/tasks/{task_id}"

    def _task_text(self, ns: Namespace, task_id: str) -> str:
        task = self.tasks.status(task_id)
        if task is None:
            return "Error: 没有该任务"
        payload = json.loads(task["payload"])
        if payload.get("user") != ns.user or payload.get("agent") != ns.agent:
            return "Error: 任务不属于当前用户"
        lines = [
            f"任务 {task_id}",
            f"类型：{task['kind']}",
            f"状态：{task['status']}",
            f"尝试：{task['attempts']}",
        ]
        if task["message"]:
            lines.append(f"说明：{task['message']}")
        return "\n".join(lines)


def _fresh_run() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _embed_text(node: Node, max_chars: int) -> str:
    parts = [node.title, node.abstract, node.overview]
    if node.body:
        parts.append(node.body)
    return "\n".join(part for part in parts if part)[: max(max_chars, 100)]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rrf_merge(primary: list[Hit], secondary: list[Hit], limit: int, k: int = 60) -> list[Hit]:
    """倒数排名融合：两路候选按名次加权合并，同名次贡献相同，URI 去重。"""
    scores: dict[str, float] = {}
    first: dict[str, Hit] = {}
    for hits in (primary, secondary):
        for rank, hit in enumerate(hits):
            scores[hit.uri] = scores.get(hit.uri, 0.0) + 1.0 / (k + rank + 1)
            first.setdefault(hit.uri, hit)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [first[uri] for uri, _score in ordered[:limit]]


def _task_id(target: str) -> Optional[str]:
    match = _TASK_URI.fullmatch(str(target or "").strip())
    return match.group(3) if match is not None else None


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", str(text or ""))
    result: list[str] = []
    for word in words:
        if word not in result:
            result.append(word)
    return result[:16]


def _split_text(text: str, size: int) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", str(text or ""))
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        if not paragraph:
            continue
        if not buffer:
            buffer = paragraph
        elif len(buffer) + len(paragraph) + 2 <= size:
            buffer = f"{buffer}\n\n{paragraph}"
        else:
            chunks.append(buffer)
            buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


def _walk(
    ns: Namespace, kind: str, rel: str, depth: int, lines: list[str], *, level: int, limit: int,
) -> None:
    if depth <= 0 or len(lines) >= limit:
        return
    for child, is_dir in ns.entries(kind, rel):
        if len(lines) >= limit:
            return
        lines.append(f"{'  ' * level}- {ns.uri(kind, child)}{'/' if is_dir else ''}")
        if is_dir:
            _walk(ns, kind, child, depth - 1, lines, level=level + 1, limit=limit)
