"""工具声明与参数校验：宿主只负责装配、确认与超时。"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from aaa.memory.protocol import MemoryIdentity, MemoryTool

_IDENTITY_KEYS = ("user_id", "agent_id", "session_id", "run_id")


def _schema(properties: dict, required: Sequence[str] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOLS: tuple[MemoryTool, ...] = (
    MemoryTool(
        name="viking_search",
        description=(
            "在 MiniViking 上下文树中按关键词检索记忆、资源与技能，结果带 viking:// URI。"
            "scope 可选 memory（记忆与会话摘要）、resource、skill 或 all。"
        ),
        parameters=_schema({
            "query": {"type": "string", "description": "检索词或问题"},
            "scope": {"type": "string", "enum": ["memory", "resource", "skill", "all"],
                      "description": "检索范围，默认 all"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                      "description": "返回条数，默认 8"},
        }, ["query"]),
        read_only=True,
    ),
    MemoryTool(
        name="viking_read",
        description=(
            "读取一个 viking:// 节点：abstract 一句话摘要、overview 概述、full 正文。"
            "正文过长时用 offset/limit 分段；也可传导入任务的 URI 查询状态。"
        ),
        parameters=_schema({
            "target": {"type": "string", "description": "viking:// URI 或任务 URI"},
            "level": {"type": "string", "enum": ["abstract", "overview", "full"],
                      "description": "读取深度，默认 abstract"},
            "offset": {"type": "integer", "minimum": 0, "description": "full 模式的起始字符"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8000,
                      "description": "full 模式的字符数，默认 4000"},
        }, ["target"]),
        read_only=True,
    ),
    MemoryTool(
        name="viking_browse",
        description=(
            "浏览 MiniViking 上下文树：tree 递归、list 列目录、stat 看节点元数据。"
            "target 为空时列出命名空间根；也可以传 viking:// URI。"
        ),
        parameters=_schema({
            "target": {"type": "string", "description": "viking:// URI；空表示根"},
            "action": {"type": "string", "enum": ["tree", "list", "stat"],
                       "description": "操作，默认 list"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 5,
                      "description": "tree 的深度，默认 2"},
        }),
        read_only=True,
    ),
    MemoryTool(
        name="viking_remember",
        description="显式写入一条长期记忆；标题与内容必填，可带标签与重要度。",
        parameters=_schema({
            "title": {"type": "string", "description": "简短标题"},
            "content": {"type": "string", "description": "自包含的正文"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "分类标签"},
            "importance": {"type": "number", "minimum": 0, "maximum": 1,
                           "description": "重要度，默认 0.5"},
        }, ["title", "content"]),
    ),
    MemoryTool(
        name="viking_forget",
        description="按精确 viking:// URI 删除一个节点文件；目录不会被递归删除。",
        parameters=_schema({
            "target": {"type": "string", "description": "要删除的节点 URI"},
        }, ["target"]),
        requires_confirmation=True,
    ),
    MemoryTool(
        name="viking_add_resource",
        description=(
            "导入本地文件或目录到 MiniViking。立即返回任务 URI，解析、切分与摘要"
            "在后台完成；用 viking_read 查任务状态。"
        ),
        parameters=_schema({
            "source": {"type": "string", "description": "本地路径（支持 ~）"},
            "kind": {"type": "string", "enum": ["auto", "file", "dir"],
                     "description": "来源类型，默认 auto"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "分类标签"},
        }, ["source"]),
        requires_confirmation=True,
    ),
    MemoryTool(
        name="viking_add_skill",
        description="写入一个技能说明（SKILL.md）。技能只是数据，不会被执行；默认不覆盖同名技能。",
        parameters=_schema({
            "name": {"type": "string", "description": "技能名"},
            "content": {"type": "string", "description": "完整的技能说明"},
            "overwrite": {"type": "boolean", "description": "是否覆盖同名技能，默认 false"},
        }, ["name", "content"]),
        requires_confirmation=True,
    ),
)


def dispatch(engine: Any, name: str, arguments: dict, identity: MemoryIdentity) -> str:
    if any(key in arguments for key in _IDENTITY_KEYS):
        return "Error: 身份由宿主提供，不能通过工具参数指定"
    try:
        if name == "viking_search":
            query = _text(arguments, "query")
            if not query:
                raise ValueError("query 不能为空")
            scope = _choice(arguments, "scope", ("memory", "resource", "skill", "all"), "all")
            limit = _int(arguments, "limit", 8, 1, 50)
            hits = engine.search(identity, query, scope=scope, limit=limit)
            if not hits:
                return "没有命中。"
            lines = []
            for index, hit in enumerate(hits, 1):
                line = f"{index}. [{hit.uri}] {hit.title}"
                if hit.abstract:
                    line += f" — {hit.abstract}"
                if hit.tags:
                    line += f" · {'、'.join(hit.tags)}"
                lines.append(line)
            return "\n".join(lines)

        if name == "viking_read":
            target = _text(arguments, "target")
            if not target:
                raise ValueError("target 不能为空")
            level = _choice(arguments, "level", ("abstract", "overview", "full"), "abstract")
            offset = _int(arguments, "offset", 0, 0, 1_000_000)
            limit = _int(arguments, "limit", 4000, 1, 8000)
            return engine.read_target(identity, target, level=level, offset=offset, limit=limit)

        if name == "viking_browse":
            target = _text(arguments, "target", required=False)
            action = _choice(arguments, "action", ("tree", "list", "stat"), "list")
            depth = _int(arguments, "depth", 2, 1, 5)
            return engine.browse(identity, target, action=action, depth=depth)

        if name == "viking_remember":
            return engine.remember(
                identity,
                title=_text(arguments, "title"),
                content=_text(arguments, "content"),
                tags=_strings(arguments, "tags"),
                importance=_number(arguments, "importance", 0.5, 0.0, 1.0),
            )

        if name == "viking_forget":
            return engine.forget(identity, _text(arguments, "target"))

        if name == "viking_add_resource":
            return engine.add_resource(
                identity,
                source=_text(arguments, "source"),
                kind=_choice(arguments, "kind", ("auto", "file", "dir"), "auto"),
                tags=_strings(arguments, "tags"),
            )

        if name == "viking_add_skill":
            return engine.add_skill(
                identity,
                name=_text(arguments, "name"),
                content=_text(arguments, "content"),
                overwrite=_bool(arguments, "overwrite", False),
            )
        return f"Error: 未知工具 {name}"
    except ValueError as exc:
        return f"Error: {exc}"


def _text(arguments: dict, key: str, *, required: bool = True) -> str:
    value = arguments.get(key)
    if value is None:
        if required:
            raise ValueError(f"缺少参数 {key}")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} 需要字符串")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{key} 不能为空")
    return text


def _int(arguments: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 需要整数")
    return min(max(value, minimum), maximum)


def _number(arguments: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 需要数字")
    return min(max(float(value), minimum), maximum)


def _bool(arguments: dict, key: str, default: bool) -> bool:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} 需要布尔值")
    return value


def _choice(arguments: dict, key: str, choices: Iterable[str], default: str) -> str:
    value = arguments.get(key)
    if value is None:
        return default
    allowed = tuple(choices)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{key} 只能是：{'、'.join(allowed)}")
    return value


def _strings(arguments: dict, key: str) -> tuple[str, ...]:
    value = arguments.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} 需要字符串数组")
    return tuple(item for item in value if item.strip())
