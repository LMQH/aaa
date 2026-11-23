"""项目级代码扩展的仓库信任：记录、指纹与交互确认。

只对「会执行代码或拉起外部进程」的项目级内容生效：
- animations、memory_engines：进程内代码；
- extensions：MCP 声明，会 spawn 外部命令（加载后每次工具调用仍逐次审批）。
纯文本（commands、skills、AGENTS.md）不经过本模块。
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

Ask = Callable[[str, Sequence[str]], bool]


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
    """已信任且指纹未变的类别。"""
    data = stored(project)
    return {
        kind for kind in KINDS
        if data.get(kind) and data[kind] == fingerprint(project, kind)
    }


def pending_kinds(project: Dict[str, Path]) -> list[str]:
    """存在文件但未信任或已变化的类别。"""
    trusted = trusted_kinds(project)
    return [kind for kind in KINDS if fingerprint(project, kind) and kind not in trusted]


def approve(project: Dict[str, Path], kinds: Sequence[str]) -> None:
    data = _load()
    root = str(project["root"])
    record = data.setdefault(root, {})
    for kind in kinds:
        value = fingerprint(project, kind)
        if value:
            record[kind] = value
    _save(data)


def revoke(root: str) -> bool:
    data = _load()
    if root not in data:
        return False
    data.pop(root)
    _save(data)
    return True


def entries() -> Dict[str, Dict[str, str]]:
    return _load()


def _ask(kind: str, files: Sequence[str]) -> bool:
    import questionary

    from .cli.ui import UI_STYLE

    shown = "、".join(files[:8]) + ("…" if len(files) > 8 else "")
    return bool(questionary.confirm(
        f"该项目包含{KIND_LABELS[kind]}代码（{shown}），是否加载并信任？",
        default=False,
        style=UI_STYLE,
    ).ask())


def ensure(project: Optional[Dict[str, Path]], *, interactive: bool, ask: Optional[Ask] = None) -> Set[str]:
    """解析本仓库可信的代码类；交互时逐类询问，非交互一律不加载未信任类。"""
    if project is None:
        return set()
    trusted = trusted_kinds(project)
    if not interactive:
        skipped = [KIND_LABELS[kind] for kind in pending_kinds(project)]
        if skipped:
            print(
                "非交互模式：项目级代码扩展未加载（" + "、".join(skipped) + "）。"
                "在交互模式下确认后才会加载。",
                file=sys.stderr,
            )
        return trusted
    ask = ask or _ask
    approved = []
    for kind in pending_kinds(project):
        if ask(kind, describe(project, kind)):
            approved.append(kind)
    if approved:
        approve(project, approved)
        trusted |= set(approved)
    return trusted


def trusted_project_dir(settings, kind: str) -> Optional[Path]:
    """设置启用且该类已信任时，返回项目级目录；否则 None。"""
    from . import plugins

    if not bool(settings.get("project.enabled")):
        return None
    project = plugins.project_dirs()
    return project[kind] if kind in trusted_kinds(project) else None
