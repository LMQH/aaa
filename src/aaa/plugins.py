"""扩展点：自定义斜杠命令、插件（MCP）、技能的发现与装配。

三个来源，全部在启动时发现、重启生效（`/plugin` 只改 `.disabled` 标记文件）：
- 命令：`~/.aaa/commands/<name>.md`，一个文件一条命令，正文是提示词模板，
  `$ARGUMENTS` 替换为命令后的剩余文本。
- 插件：`~/.aaa/extensions/<name>/extension.toml`，`[mcp]` 段声明一个 MCP
  server（stdio 的 `command`，或远程的 `url` + `transport`）；目录里存在
  `.disabled` 标记文件时停用。
- 技能：装配 `agno.skills.Skills`（loader 顺序 = 优先级倒序，后者覆盖）。
  发现、披露与读取（`get_skill_instructions`/`get_skill_reference`/
  `get_skill_script`）全部由 Agno 原生承担，aaa 只提供目录与来源标签。
  技能包：根下的分组目录就是包（平铺是无包），`PACK.md` 给描述，`.disabled`
  整包停用；索引里包渲染成「名字 + 描述 + 成员名单」（names-only，描述省、
  名字不省），包不改技能身份，也不参与仓库信任门控。

MCP 桥接：agno 的 MCPTools 只支持异步上下文，而 aaa 的 agent 是同步运行。
这里用一个常驻后台事件循环连接 server，把每个异步 Function 的入口换成
`run_coroutine_threadsafe(...).result(timeout)` 的同步桥。第三方 MCP 工具
是外部信任面的代码：名字统一加 `mcp_` 前缀，`boundary.is_external_tool`
据此判定「每次询问、审批策略不自动放行」。
"""

import asyncio
import logging
import re
import threading
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

from agno.skills import Skills
from agno.skills.loaders.local import LocalSkills
from agno.tools.function import Function

from .extensions.loader import PythonExtension, discover_python_extensions

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------- 技能包


@dataclass(frozen=True)
class SkillPack:
    """技能包：根下的一个分组目录；`PACK.md` 给描述，`.disabled` 整包停用。

    包是目录组织投射出的视图：包名就是目录名，成员是包内任意深度的技能，
    平铺在根上的技能不属于任何包。
    """

    name: str
    path: Path
    description: str = ""
    enabled: bool = True
    members: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _SkillEntry:
    """一个待装配的技能目录：路径、来源、生效名与所属包。"""

    path: Path
    source: str
    name: str
    pack_name: Optional[str]
    pack_dir: Optional[Path]


SKILL_SUPPORT_DIRS = frozenset({"references", "scripts", "templates", "assets"})
_SKILL_BLOCK_RE = re.compile(r"<skill>\n.*?\n</skill>\n?", re.DOTALL)


def iter_skill_dirs(root: Path) -> List[Path]:
    """任意深度找技能目录：含 `SKILL.md` 的目录是技能，且不再下钻。

    点目录与符号链接目录跳过；没有 `SKILL.md` 的分组目录（技能包）继续下钻；
    技能自身的 `references/` 等支持目录因「含 SKILL.md 即止」不会成为技能根。
    """
    found: List[Path] = []

    def walk(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        if any(child.name == "SKILL.md" and child.is_file() for child in children):
            found.append(directory)
            return
        for child in children:
            if child.name.startswith(".") or child.name in SKILL_SUPPORT_DIRS:
                continue
            if child.is_dir() and not child.is_symlink():
                walk(child)

    walk(root)
    return found


def _pack_description(pack_dir: Path) -> str:
    """包目录里 `PACK.md` 的 `description`；缺失或读不了时为空串。"""
    try:
        meta, _ = _frontmatter((pack_dir / "PACK.md").read_text(encoding="utf-8", errors="replace"))
    except (OSError, UnicodeError):
        return ""
    return str(meta.get("description") or "").strip()


class PackSkills(GatedSkills):
    """带技能包视图的技能束：索引里每个包渲染成「名字 + 描述 + 成员名单」。

    names-only（Hermes 式）：包内技能的描述不进索引，但名字全部保留——
    模型不会主动二次发现索引里消失的东西，名字可见即可直接
    `get_skill_instructions(成员名)`。仅在装配点使用，不改 Agno 执行路径。
    """

    def __init__(self, loaders, *, member_pack: Dict[str, str],
                 packs: Sequence[SkillPack], allow_scripts: bool = True):
        super().__init__(loaders, allow_scripts=allow_scripts)
        self._member_pack = member_pack
        self._packs = list(packs)

    def without_scripts(self) -> "PackSkills":
        """read-only 变体：共享已装配的技能，只收起脚本工具与宣传。"""
        clone = PackSkills(
            self.loaders, member_pack=self._member_pack, packs=self._packs,
            allow_scripts=False,
        )
        clone._skills = dict(self._skills)
        return clone

    def get_system_prompt_snippet(self) -> str:
        snippet = super().get_system_prompt_snippet()
        if not self._packs:
            return snippet
        members: Dict[str, List[str]] = {pack.name: [] for pack in self._packs}

        def take(match: "re.Match[str]") -> str:
            block = match.group(0)
            found = re.search(r"<name>(.*?)</name>", block)
            name = found.group(1) if found else ""
            pack_name = self._member_pack.get(name)
            if not pack_name:
                return block
            if pack_name in members:
                members[pack_name].append(name)
            return ""

        stripped = _SKILL_BLOCK_RE.sub(take, snippet)
        pack_lines: List[str] = []
        for pack in self._packs:
            names = sorted(members.get(pack.name) or [])
            if not names:
                continue
            pack_lines.append("  <skillpack>")
            pack_lines.append(f"    <name>{_xml_escape(pack.name)}</name>")
            if pack.description:
                pack_lines.append(f"    <description>{_xml_escape(pack.description)}</description>")
            pack_lines.append(f"    <members>{_xml_escape(', '.join(names))}</members>")
            pack_lines.append("  </skillpack>")
        if not pack_lines:
            return snippet
        note = ("  (<skillpack> 是技能分组：成员照常可用，"
                "用 get_skill_instructions(成员名) 读取正文。)")
        return stripped.replace("</skills_system>", "\n".join([*pack_lines, note, "</skills_system>"]))


# ---------------------------------------------------------------- 技能装配


def _skill_name(folder: Path) -> str:
    """Agno 生效的技能名：frontmatter 的 `name`，缺省目录名。"""
    try:
        meta, _ = _frontmatter((folder / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    except (OSError, UnicodeError):
        return folder.name
    return str(meta.get("name") or "").strip() or folder.name


def _scan_skills(skill_dirs) -> List[_SkillEntry]:
    """按给定顺序扫出各根下的技能目录；顺序即来源标签先到先得的顺序。

    平铺 `<根>/<名>/SKILL.md` 是无包技能；更深的技能归 `<根>/<包>/…`，
    包名取根下第一段目录名。
    """
    entries: List[_SkillEntry] = []
    for d, source in _pairs(skill_dirs):
        if not _is_dir_quiet(d):
            continue
        base = Path(d)
        for folder in iter_skill_dirs(base):
            relative = folder.relative_to(base)
            pack_name = relative.parts[0] if len(relative.parts) > 1 else None
            pack_dir = base / pack_name if pack_name else None
            entries.append(_SkillEntry(folder, source, _skill_name(folder), pack_name, pack_dir))
    return entries


def _entry_enabled(entry: _SkillEntry) -> bool:
    """技能自身与所属包的 `.disabled` 标记都看；有任何一个即停用。"""
    return not (entry.path / ".disabled").exists() and (
        entry.pack_dir is None or not (entry.pack_dir / ".disabled").exists()
    )


def _assemble(entries: Sequence[_SkillEntry]) -> Tuple[Optional[Skills], Dict[str, str]]:
    """每个技能目录一个 loader，倒序交给 Agno：后者覆盖，等效先到先得的优先级。"""
    if not entries:
        return None, {}
    sources: Dict[str, str] = {}
    member_pack: Dict[str, str] = {}
    for entry in entries:
        sources.setdefault(entry.name, entry.source)
        # 平铺赢家也登记（记 ""），防止排在后面的包内同名技能认领归属。
        if entry.name not in member_pack:
            member_pack[entry.name] = entry.pack_name or ""
    loaders = [LocalSkills(str(e.path), validate=False) for e in reversed(entries)]
    return PackSkills(loaders, member_pack=member_pack, packs=pack_infos(entries)), sources


def build_skills(skill_dirs) -> Tuple[Optional[Skills], Dict[str, str]]:
    """装配 `agno.skills.Skills`；返回 (技能束, 技能名→来源)。

    发现任意深度（Hermes 语义）：含 `SKILL.md` 的目录是技能且不再下钻；
    平铺是无包技能，`<根>/<包>/…` 归该包，包目录的 `.disabled` 整包停用，
    技能目录的 `.disabled` 停用单个技能。`skill_dirs` 按 [builtin, user…,
    project] 给出：loader 顺序是优先级的倒序，Agno 同名技能「后者覆盖」，
    等效 aaa 的先到先得（project > user > builtin）。校验保持宽容：
    `validate=False`，非法技能跳过而不是中止装配。来源标签先到先得，
    键为 Agno 生效的技能名（frontmatter `name`，缺省目录名）。
    """
    return _assemble([e for e in _scan_skills(skill_dirs) if _entry_enabled(e)])


def pack_infos(entries: Sequence[_SkillEntry]) -> List[SkillPack]:
    """扫到的技能包视图（含停用的包），按首次出现排序；同名技能只计一次。"""
    ordered: Dict[str, SkillPack] = {}
    members: Dict[str, List[str]] = {}
    seen: set = set()
    for entry in entries:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        if entry.pack_name is None:
            continue
        if entry.pack_name not in ordered:
            ordered[entry.pack_name] = SkillPack(
                name=entry.pack_name,
                path=entry.pack_dir,
                description=_pack_description(entry.pack_dir),
                enabled=not (entry.pack_dir / ".disabled").exists(),
            )
            members[entry.pack_name] = []
        members[entry.pack_name].append(entry.name)
    return [replace(pack, members=tuple(members[pack.name])) for pack in ordered.values()]


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
    skill_packs: Dict[str, str] = field(default_factory=dict)
    pack_info: List[SkillPack] = field(default_factory=list)


def discover_extensions(
    project: Optional[Dict[str, Path]],
    *,
    trusted: frozenset = frozenset(),
    skills_user_dirs: Sequence[Path] = (),
) -> ExtensionDiscovery:
    """统一发现入口（aaa 翻译层）：命令、技能、MCP、编程扩展、动画目录。

    `trusted` 是已确认的仓库信任类别；项目级 skills/extensions/animations
    仅在对应类别受信任时纳入。技能包由技能根的目录结构投射，包目录的
    `.disabled` 停用整包。
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
    entries = _scan_skills(skill_dirs)
    skills, skill_sources = _assemble([e for e in entries if _entry_enabled(e)])
    skill_packs: Dict[str, str] = {}
    for entry in entries:
        if not _entry_enabled(entry):
            continue
        # 与 _assemble 的 member_pack 同一规则：装配赢家定归属，平铺赢家记 ""（无包）。
        if entry.name not in skill_packs:
            skill_packs[entry.name] = entry.pack_name or ""

    extension_dirs = [(EXTENSIONS_DIR_DEFAULT, "user")]
    if project and "extensions" in trusted:
        extension_dirs.insert(0, (project["extensions"], "project"))
    mcp = discover_mcp(extension_dirs)
    python = discover_python_extensions(extension_dirs)

    animations_dir = project["animations"] if project and "animations" in trusted else None
    return ExtensionDiscovery(
        commands=commands, skills=skills, skill_sources=skill_sources,
        skill_packs=skill_packs,
        pack_info=pack_infos(entries),
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
