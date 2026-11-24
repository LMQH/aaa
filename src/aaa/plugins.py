"""扩展点：自定义斜杠命令、插件（MCP）、技能的发现与装配。

三个来源，全部在启动时发现、重启生效（`/plugin` 只改标记文件）：
- 命令：`~/.aaa/commands/<name>.md`，一个文件一条命令，正文是提示词模板，
  `$ARGUMENTS` 替换为命令后的剩余文本。
- 插件：`~/.aaa/extensions/<name>/extension.toml`，`[mcp]` 段声明一个 MCP
  server（stdio 的 `command`，或远程的 `url` + `transport`）；目录里存在
  `.disabled` 标记文件时停用。
- 技能：装配 `agno.skills.Skills`（loader 顺序 = 优先级倒序，后者覆盖）。
  发现、披露与读取（`get_skill_instructions`/`get_skill_reference`/
  `get_skill_script`）全部由 Agno 原生承担，aaa 只提供目录与来源标签。

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
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from agno.skills import Skills
from agno.skills.loaders.local import LocalSkills
from agno.tools.function import Function

from .extensions.loader import PythonExtension, discover_python_extensions


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


class GatedSkills(Skills):
    """按权限模式裁剪的技能束：不允许脚本时移除执行工具与索引里的脚本宣传。

    仅在装配点使用（runtime 按 mode 选择变体），不改 Agno 的执行路径。
    """

    def __init__(self, loaders, *, allow_scripts: bool):
        super().__init__(loaders)
        self._allow_scripts = allow_scripts

    def get_tools(self) -> List[Function]:
        tools = super().get_tools()
        if self._allow_scripts:
            return tools
        return [tool for tool in tools if getattr(tool, "name", "") != "get_skill_script"]

    def get_system_prompt_snippet(self) -> str:
        snippet = super().get_system_prompt_snippet()
        if self._allow_scripts:
            return snippet
        lines = []
        for line in snippet.splitlines():
            stripped = line.strip()
            if "get_skill_script" in stripped:
                continue
            if stripped.startswith("<scripts>") and stripped != "<scripts>none</scripts>":
                line = line.replace(stripped, "<scripts>none</scripts>")
            lines.append(line)
        return "\n".join(lines)


def build_skills(skill_dirs) -> Tuple[Optional[Skills], Dict[str, str]]:
    """装配 `agno.skills.Skills`；返回 (技能束, 技能名→来源)。

    loader 顺序是优先级的倒序：Agno 同名技能「后者覆盖」，因此把目录按
    [builtin, user…, project] 传入，等效 aaa 的先到先得（project > user > builtin）。
    校验保持 aaa 的宽容语义：`validate=False`，非法技能跳过而不是中止装配。
    发现只扫一层 `<dir>/<name>/SKILL.md`；来源标签按原顺序先到先得，
    键为 Agno 生效的技能名（frontmatter `name`，缺省目录名）。
    """
    ordered = list(_pairs(skill_dirs))
    loaders = [LocalSkills(str(d), validate=False) for d, _ in reversed(ordered) if _is_dir_quiet(d)]
    sources: Dict[str, str] = {}
    for d, source in ordered:
        if not _is_dir_quiet(d):
            continue
        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            # Agno 的技能名取 frontmatter 的 name，缺省目录名；来源标签按同名键记录。
            name = child.name
            try:
                meta, _ = _frontmatter((child / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
                front = str(meta.get("name") or "").strip()
                if front:
                    name = front
            except (OSError, UnicodeError):
                pass
            sources.setdefault(name, source)
    if not loaders:
        return None, {}
    return Skills(loaders), sources


def _is_dir_quiet(d) -> bool:
    try:
        return Path(d).is_dir()
    except OSError:
        return False


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


@dataclass(frozen=True)
class ExtensionDiscovery:
    """五类扩展的统一发现结果；只做扫描与信任门控，激活由宿主进行。"""

    commands: Dict[str, CustomCommand]
    skills: Optional[Skills]
    skill_sources: Dict[str, str]
    mcp: List[McpExtension]
    python: List[PythonExtension]
    animations_dir: Optional[Path]


def discover_extensions(
    project: Optional[Dict[str, Path]],
    *,
    trusted: frozenset = frozenset(),
    skills_user_dirs: Sequence[Path] = (),
) -> ExtensionDiscovery:
    """统一发现入口（aaa 翻译层）：命令、技能、MCP、编程扩展、动画目录。

    `trusted` 是已确认的仓库信任类别；项目级 skills/extensions/animations
    仅在对应类别受信任时纳入。
    """
    command_dirs = ([(project["commands"], "project")] if project else []) + [(COMMANDS_DIR_DEFAULT, "user")]
    commands: Dict[str, CustomCommand] = {}
    for command in discover_commands(command_dirs):
        commands.setdefault(command.name, command)

    skill_dirs = (
        ([(project["skills"], "project")] if project and "skills" in trusted else [])
        + [(d, "user") for d in skills_user_dirs]
        + [(BUILTIN_SKILLS_DIR, "builtin")]
    )
    skills, skill_sources = build_skills(skill_dirs)

    extension_dirs = [(EXTENSIONS_DIR_DEFAULT, "user")]
    if project and "extensions" in trusted:
        extension_dirs.insert(0, (project["extensions"], "project"))
    mcp = discover_mcp(extension_dirs)
    python = discover_python_extensions(extension_dirs)

    animations_dir = project["animations"] if project and "animations" in trusted else None
    return ExtensionDiscovery(
        commands=commands, skills=skills, skill_sources=skill_sources,
        mcp=mcp, python=python, animations_dir=animations_dir,
    )


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
        return f"已创建 skill:{name} → {target}。重启生效；/skills 可见，模型可用 get_skill_instructions 读取。"

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
