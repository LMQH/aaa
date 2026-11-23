"""扩展点：自定义斜杠命令、插件（MCP）、技能的发现与装配。

三个来源，全部在启动时发现、重启生效（`/plugin` 只改标记文件）：
- 命令：`~/.aaa/commands/<name>.md`，一个文件一条命令，正文是提示词模板，
  `$ARGUMENTS` 替换为命令后的剩余文本。
- 插件：`~/.aaa/extensions/<name>/extension.toml`，`[mcp]` 段声明一个 MCP
  server（stdio 的 `command`，或远程的 `url` + `transport`）；目录里存在
  `.disabled` 标记文件时停用。
- 技能：`skills_dirs` 下 `<name>/SKILL.md`（支持一层分类目录），YAML
  frontmatter 的 `name`/`description` 用于展示；正文经 `load_skill` 工具
  由模型按需读取。

MCP 桥接：agno 的 MCPTools 只支持异步上下文，而 aaa 的 agent 是同步运行。
这里用一个常驻后台事件循环连接 server，把每个异步 Function 的入口换成
`run_coroutine_threadsafe(...).result(timeout)` 的同步桥。第三方 MCP 工具
是外部信任面的代码：名字统一加 `mcp_` 前缀，`boundary.is_external_tool`
据此判定「每次询问、审批策略不自动放行」。
"""

import asyncio
import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from agno.exceptions import PathSecurityError
from agno.tools.function import Function
from agno.utils.path_safety import safe_join_relative_path


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...

COMMAND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
MCP_CALL_TIMEOUT_FALLBACK = 120


@dataclass(frozen=True)
class CustomCommand:
    name: str
    path: Path
    template: str
    source: str = "user"


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    description: str
    source: str = "user"


@dataclass(frozen=True)
class McpExtension:
    name: str
    path: Path
    spec: Dict[str, Any]
    enabled: bool
    source: str = "user"


def _pairs(dirs):
    """目录项可为 Path 或 (Path, 来源)；纯 Path 视为 user 级。"""
    for item in dirs:
        if isinstance(item, tuple):
            yield Path(item[0]), str(item[1])
        else:
            yield Path(item), "user"


def project_root(start: Optional[Path] = None) -> Path:
    """向上找含 .git 的项目根；没有 git 时取起点自身。"""
    current = (Path(start) if start is not None else Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def project_dirs(start: Optional[Path] = None) -> Dict[str, Path]:
    """项目级扩展目录与 AGENTS.md 位置。"""
    root = project_root(start)
    base = root / ".aaa"
    return {
        "root": root,
        "commands": base / "commands",
        "skills": base / "skills",
        "extensions": base / "extensions",
        "animations": base / "animations",
        "memory_engines": base / "memory_engines",
        "agents_md": root / "AGENTS.md",
    }


def discover_commands(dirs) -> List[CustomCommand]:
    out: List[CustomCommand] = []
    for d, source in _pairs(dirs):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if not COMMAND_NAME.fullmatch(f.stem):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            out.append(CustomCommand(f.stem, f, text, source))
    return out


def _frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    header, _, body = rest.partition("---")
    meta: Dict[str, Any] = {}
    key: Optional[str] = None
    for line in header.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and key:
            meta[key] = f"{meta[key]} {line.strip()}".strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            meta[key] = value.strip().strip('"').strip("'")
    return meta, body.strip()


def discover_skills(dirs) -> List[Skill]:
    """`<dir>/<name>/SKILL.md` 与 `<dir>/<分类>/<name>/SKILL.md` 两层都扫；先到先得。"""
    out: List[Skill] = []
    seen: set = set()
    for d, source in _pairs(dirs):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*/SKILL.md")) + sorted(d.glob("*/*/SKILL.md")):
            name = f.parent.name
            if name in seen or (name == "references" and f.parent.is_symlink()):
                continue
            try:
                body_path = safe_join_relative_path(f.parent, f.name)
                if not body_path.is_file():
                    continue
                meta, _ = _frontmatter(body_path.read_text(encoding="utf-8"))
            except (PathSecurityError, OSError, RuntimeError, UnicodeError):
                continue
            seen.add(name)
            out.append(Skill(name, f, str(meta.get("description", "")), source))
    return out


def skill_body(path: Path) -> str:
    """SKILL.md 去掉 frontmatter 的正文；模型看到的就是它。"""
    source = safe_join_relative_path(path.parent, path.name)
    if not source.is_file():
        raise FileNotFoundError(path)
    _, body = _frontmatter(source.read_text(encoding="utf-8", errors="replace"))
    return body


def discover_mcp(dirs) -> List[McpExtension]:
    out: List[McpExtension] = []
    for d, source in _pairs(dirs):
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            manifest = sub / "extension.toml"
            if not sub.is_dir() or not manifest.is_file():
                continue
            try:
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            spec = data.get("mcp")
            if not isinstance(spec, dict) or not (spec.get("command") or spec.get("url")):
                continue
            out.append(McpExtension(
                name=sub.name, path=sub, spec=spec,
                enabled=not (sub / ".disabled").exists(),
                source=source,
            ))
    return out


def toggle_extension(path: Path) -> bool:
    """切换插件启停：增删 `.disabled` 标记；返回切换后的启用状态。"""
    marker = path / ".disabled"
    if marker.exists():
        marker.unlink()
        return True
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    return False


# ---------------------------------------------------------------- MCP 桥接

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()


def _mcp_loop() -> asyncio.AbstractEventLoop:
    """MCP 会话的常驻事件循环：后台线程里跑，进程生命周期一致。"""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="aaa-mcp").start()
        return _loop


def _bridge(entrypoint, timeout: float):
    """把异步 MCP 工具入口换成同步桥：调度回 MCP 事件循环并等待结果。"""
    def run(**kwargs):
        fut = asyncio.run_coroutine_threadsafe(entrypoint(**kwargs), _mcp_loop())
        return fut.result(timeout=timeout)
    run.__name__ = getattr(entrypoint, "__name__", "mcp_tool").rsplit(".", 1)[-1]
    return run


def open_mcp_tools(exts: List[McpExtension], timeout: float) -> Tuple[List[Function], List[str]]:
    """连接启用的 MCP 插件，返回（同步桥接后的 Function 列表, 失败名单）。

    连接失败只记名字不中断启动；桥接超时与连接共用 `timeout`（秒）。
    """
    from agno.tools.mcp import MCPTools

    functions: List[Function] = []
    failed: List[str] = []
    for ext in exts:
        if not ext.enabled:
            continue
        spec = ext.spec
        try:
            async def _open():
                kwargs: Dict[str, Any] = {"timeout_seconds": int(timeout)}
                if spec.get("url"):
                    kwargs["url"] = spec["url"]
                    if spec.get("transport"):
                        kwargs["transport"] = spec["transport"]
                else:
                    kwargs["command"] = str(spec["command"])
                if isinstance(spec.get("env"), dict):
                    kwargs["env"] = {str(k): str(v) for k, v in spec["env"].items()}
                mcp_tools = MCPTools(**kwargs)
                await mcp_tools.__aenter__()
                return mcp_tools

            mcp_tools = asyncio.run_coroutine_threadsafe(_open(), _mcp_loop()).result(timeout=timeout + 10)
        except Exception:
            failed.append(ext.name)
            continue
        for f in mcp_tools.functions.values():
            f.entrypoint = _bridge(f.entrypoint, timeout)
            f.name = f"mcp_{f.name}"[:64]
            f.requires_confirmation = True
            functions.append(f)
    return functions, failed


def make_skill_tool(skills: List[Skill]):
    """`load_skill` 只读已发现技能的正文或 Markdown 引用。"""
    index = {s.name: s for s in skills}

    def load_skill(name: str, reference: Optional[str] = None) -> str:
        """读取已发现技能的正文，或按技能指引读取一份 Markdown 引用。

        Args:
            name: 系统提示词技能目录中的技能名。
            reference: 可选的引用文件名（含 .md，不传则读技能正文）。
        """
        skill = index.get(name)
        if skill is None:
            known = "、".join(sorted(index)) or "（无）"
            return f"未知技能：{name}。可用：{known}"
        if reference is not None:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\.md", reference):
                return "引用必须是单个 Markdown 文件名。"
            try:
                if (skill.path.parent / "references").is_symlink():
                    return f"无法读取技能 {name} 的引用：{reference}"
                root = safe_join_relative_path(skill.path.parent, "references")
                # 源码运行与 wheel 安装共享 docs/extending/ 的内容；用户同名技能不继承内置资料。
                if (skill.path == BUILTIN_SKILLS_DIR / "create-extension" / "SKILL.md"
                        and not root.exists() and BUILTIN_SKILLS_DIR.parent.parent.name == "src"):
                    root = BUILTIN_SKILLS_DIR.parents[2] / "docs" / "extending"
                target = safe_join_relative_path(root, reference)
                if not target.is_file():
                    return f"技能 {name} 没有引用：{reference}"
                return target.read_text(encoding="utf-8")
            except (PathSecurityError, OSError, RuntimeError, UnicodeError):
                return f"无法读取技能 {name} 的引用：{reference}"
        try:
            return skill_body(skill.path)
        except (PathSecurityError, OSError, RuntimeError):
            return f"无法读取技能 {name} 的正文。"

    return load_skill


COMMANDS_DIR_DEFAULT = Path.home() / ".aaa" / "commands"
EXTENSIONS_DIR_DEFAULT = Path.home() / ".aaa" / "extensions"
SKILLS_ROOT_DEFAULT = Path.home() / ".agents" / "skills"
BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"
COMMAND_NAME_RE = COMMAND_NAME


def make_create_tools():
    """agent 创建插件的写入口：`create_plugin`（命令/MCP）与 `create_skill`（技能）。

    路径各自锁死（~/.aaa 与 ~/.agents/skills），逐次确认由
    `requires_confirmation` 保证；写入后提示重启生效。
    """
    def create_plugin(kind: str, name: str, content: str) -> str:
        """安装一个 aaa 插件：自定义命令或 MCP 插件。

        Args:
            kind: "command"（自定义斜杠命令）或 "extension"（MCP 插件）。
            name: 插件名；命令名须为 [a-z0-9][a-z0-9_-]*，且不与内置命令重名。
            content: 文件全文——command 是提示词模板；extension 是含 [mcp] 段的 TOML。
        """
        if kind not in ("command", "extension"):
            return f"未知类型：{kind}（应为 command 或 extension）"
        if not COMMAND_NAME_RE.fullmatch(name or ""):
            return f"名称不合法：{name}（须为 [a-z0-9][a-z0-9_-]*）"
        if not content or not content.strip():
            return "内容为空。"
        try:
            if kind == "command":
                target = COMMANDS_DIR_DEFAULT / f"{name}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            else:
                parsed = tomllib.loads(content)
                if not isinstance(parsed.get("mcp"), dict) or not (
                    parsed["mcp"].get("command") or parsed["mcp"].get("url")
                ):
                    return "TOML 缺少 [mcp] 段或其中没有 command/url。"
                target = EXTENSIONS_DIR_DEFAULT / name / "extension.toml"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return f"创建失败：{exc}"
        hint = "重启后即可使用 /%s" % name if kind == "command" else "重启生效；/plugin 可见"
        return f"已创建 {kind}:{name} → {target}。{hint}。"

    def create_skill(name: str, content: str, force: bool = False) -> str:
        """安装一个技能到 ~/.agents/skills/<name>/SKILL.md。

        Args:
            name: 技能名，[a-z0-9][a-z0-9_-]*；与目录名一致。
            content: SKILL.md 全文，必须以 YAML frontmatter 开头并含 name 与
                description（description 说明什么场景该读它）。
            force: 同名技能已存在时是否覆盖（默认拒绝，避免静默改写已有技能）。
        """
        if not COMMAND_NAME_RE.fullmatch(name or ""):
            return f"名称不合法：{name}（须为 [a-z0-9][a-z0-9_-]*）"
        if not content or not content.strip():
            return "内容为空。"
        meta, body = _frontmatter(content)
        if not body.strip():
            return "缺少正文：frontmatter 之后应有技能内容。"
        if meta.get("name") != name:
            return f"frontmatter 的 name（{meta.get('name')!r}）须与技能名一致：{name}"
        if not meta.get("description"):
            return "frontmatter 缺少 description（说明什么场景该读这个技能）。"
        target = SKILLS_ROOT_DEFAULT / name / "SKILL.md"
        if target.exists() and not force:
            return f"技能 {name} 已存在（{target}）。要覆盖请带 force=true，并先向用户确认。"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"创建失败：{exc}"
        return f"已创建 skill:{name} → {target}。重启生效；/skills 可见，模型可用 load_skill 读取。"

    create_plugin_fn = Function(
        name="create_plugin",
        description=(
            "安装 aaa 插件：kind=command 装自定义斜杠命令（提示词模板），"
            "kind=extension 装 MCP 插件（[mcp] 段的 TOML）。安装需用户确认，重启 aaa 生效。"
        ),
        entrypoint=create_plugin,
        requires_confirmation=True,
    )
    create_skill_fn = Function(
        name="create_skill",
        description=(
            "安装 aaa 技能：把 SKILL.md 全文（frontmatter 含 name/description）写到"
            " ~/.agents/skills/<名>/。同名已存在时默认拒绝（force=true 覆盖，需先向用户确认）。"
            "安装需用户确认，重启 aaa 生效。"
        ),
        entrypoint=create_skill,
        requires_confirmation=True,
    )
    return create_plugin_fn, create_skill_fn


def mcp_timeout(settings: SettingsLike) -> float:
    try:
        return float(settings.get("extensions.mcp_timeout"))
    except Exception:
        return MCP_CALL_TIMEOUT_FALLBACK
