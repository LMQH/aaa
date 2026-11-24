"""项目级代码扩展的仓库信任：记录、指纹与交互确认。

只对「会执行代码或拉起外部进程」的项目级内容生效：
- animations、memory_engines：进程内代码；
- extensions：MCP 声明，会 spawn 外部命令（加载后每次工具调用仍逐次审批）。
纯文本（commands、skills、AGENTS.md）不经过本模块。

记录值是以下三种之一：`<指纹>` 只信任该内容（内容变化后重新询问）、`*` 始终信任、
`!<指纹>` 该内容已拒绝（内容变化后重新询问）。交互启动时把全部待决类别合并成一次询问，
可选「不加载 / 仅信任当前内容 / 始终信任此仓库」；`--trust-repo` 直接按「始终」批准。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Set

from .config.paths import TRUST_FILE

KINDS = ("animations", "memory_engines", "extensions")
KIND_LABELS = {"animations": "启动动画", "memory_engines": "记忆引擎", "extensions": "MCP 扩展"}
# 记录值：`<指纹>` 只信任该内容；`*` 始终信任；`!<指纹>` 该内容已拒绝。
ALWAYS = "*"
DENY_PREFIX = "!"
POLICIES = ("once", "always", "deny")

Ask = Callable[[Sequence[str], str], str]


def _kind_files(project: Dict[str, Path], kind: str) -> list[Path]:
    base = project[kind]
    if not base.is_dir():
        return []
    if kind in ("animations", "memory_engines"):
        return sorted(p for p in base.glob("*.py") if p.is_file())
    files: list[Path] = []
    for sub in sorted(base.iterdir()):
        if not sub.is_dir() or not (sub / "extension.toml").is_file():
            continue
        files.extend(sorted(p for p in sub.rglob("*") if p.is_file()))
    return files


def fingerprint(project: Dict[str, Path], kind: str) -> str:
    """该类全部文件按相对路径排序后的内容指纹；无文件返回空串。"""
    files = _kind_files(project, kind)
    if not files:
        return ""
    digest = hashlib.sha256()
    root = project["root"]
    for path in files:
        try:
            rel = str(path.relative_to(root))
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def describe(project: Dict[str, Path], kind: str) -> list[str]:
    root = project["root"]
    out = []
    for path in _kind_files(project, kind):
        try:
            out.append(str(path.relative_to(root)))
        except ValueError:
            out.append(str(path))
    return out


def _load() -> Dict[str, Dict[str, str]]:
    try:
        raw = json.loads(TRUST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for root, kinds in raw.items():
        if isinstance(kinds, dict):
            result[str(root)] = {str(k): str(v) for k, v in kinds.items()}
    return result


def _save(data: Dict[str, Dict[str, str]]) -> None:
    TRUST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRUST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, TRUST_FILE)


def stored(project: Dict[str, Path]) -> Dict[str, str]:
    return _load().get(str(project["root"]), {})


def trusted_kinds(project: Dict[str, Path]) -> Set[str]:
    """已信任的类别：始终信任，或内容指纹未变。"""
    data = stored(project)
    result = set()
    for kind in KINDS:
        value = data.get(kind)
        if value == ALWAYS or (value and value == fingerprint(project, kind)):
            result.add(kind)
    return result


def denied_kinds(project: Dict[str, Path]) -> Set[str]:
    """当前内容已被拒绝的类别；内容变化后会重新询问。"""
    data = stored(project)
    return {
        kind for kind in KINDS
        if (value := data.get(kind)) and value.startswith(DENY_PREFIX)
        and value[len(DENY_PREFIX):] == fingerprint(project, kind)
    }


def pending_kinds(project: Dict[str, Path]) -> list[str]:
    """存在文件、且从未表态或内容已变化的类别。"""
    trusted = trusted_kinds(project)
    denied = denied_kinds(project)
    return [
        kind for kind in KINDS
        if fingerprint(project, kind) and kind not in trusted and kind not in denied
    ]


def _record(project: Dict[str, Path], kinds: Sequence[str], value: Callable[[str], str]) -> None:
    data = _load()
    record = data.setdefault(str(project["root"]), {})
    for kind in kinds:
        encoded = value(fingerprint(project, kind))
        if encoded:
            record[kind] = encoded
    _save(data)


def approve(project: Dict[str, Path], kinds: Sequence[str], policy: str = "once") -> None:
    if policy not in ("once", "always"):
        raise ValueError(f"未知信任策略：{policy}")
    _record(project, kinds, lambda _fp: ALWAYS if policy == "always" else _fp)


def deny(project: Dict[str, Path], kinds: Sequence[str]) -> None:
    _record(project, kinds, lambda fp: DENY_PREFIX + fp)


def status(project: Dict[str, Path], kind: str) -> str:
    """empty / always / trusted / denied / pending。"""
    if not fingerprint(project, kind):
        return "empty"
    value = stored(project).get(kind)
    if value == ALWAYS:
        return "always"
    if value and value.startswith(DENY_PREFIX):
        return "denied" if kind in denied_kinds(project) else "pending"
    if value and kind in trusted_kinds(project):
        return "trusted"
    return "pending"


def revoke(root: str) -> bool:
    data = _load()
    if root not in data:
        return False
    data.pop(root)
    _save(data)
    return True


def entries() -> Dict[str, Dict[str, str]]:
    return _load()


_CHOICES = {"不加载": "deny", "仅信任当前内容": "once", "始终信任此仓库": "always"}


def summary(project: Dict[str, Path], kinds: Sequence[str]) -> str:
    lines = ["该仓库包含可执行扩展："]
    for kind in kinds:
        files = describe(project, kind)
        shown = "、".join(files[:4]) + ("…" if len(files) > 4 else "")
        lines.append(f"  {KIND_LABELS[kind]}（{len(files)} 个文件）：{shown}")
    return "\n".join(lines)


def _ask(kinds: Sequence[str], text: str) -> str:
    import questionary

    from .cli.ui import UI_STYLE

    choice = questionary.select(
        text,
        choices=list(_CHOICES),
        default="不加载",
        style=UI_STYLE,
    ).ask()
    return _CHOICES.get(choice, "deny")


def ensure(
    project: Optional[Dict[str, Path]],
    *,
    interactive: bool,
    always: bool = False,
    ask: Optional[Ask] = None,
) -> Set[str]:
    """解析本仓库可信的代码类；交互时对全部待决类别合并询问一次。"""
    if project is None:
        return set()
    pending = pending_kinds(project)
    if pending and always:
        approve(project, pending, policy="always")
        pending = []
    if not pending:
        return trusted_kinds(project)
    if not interactive:
        skipped = [KIND_LABELS[kind] for kind in pending]
        print(
            "非交互模式：项目级代码扩展未加载（" + "、".join(skipped) + "）。"
            "用 --trust-repo 或交互模式确认后才会加载。",
            file=sys.stderr,
        )
        return trusted_kinds(project)
    choice = (ask or _ask)(pending, summary(project, pending))
    if choice == "always":
        approve(project, pending, policy="always")
    elif choice == "once":
        approve(project, pending, policy="once")
    else:
        deny(project, pending)
    return trusted_kinds(project)


def trusted_project_dir(settings, kind: str) -> Optional[Path]:
    """设置启用且该类已信任时，返回项目级目录；否则 None。"""
    from . import plugins

    if not bool(settings.get("project.enabled")):
        return None
    project = plugins.project_dirs()
    return project[kind] if kind in trusted_kinds(project) else None
