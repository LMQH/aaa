"""文件与检索类工具：在 Agno Workspace 上加 aaa 的路径与检索语义，按能力清单装配。

能力名（read / list / write / edit / move / delete / grep）由 `boundary.policy` 决定。
"""

import fnmatch
import json
import os
import re
import time
from pathlib import Path
from typing import Collection, Dict, Iterator, List, Optional

from agno.tools.workspace import Workspace

# Workspace 能提供的能力；顺序即装配顺序。
FILE_TOOLS = ("read", "list", "write", "edit", "move", "delete")
# 单文件读取上限的默认值；实际值由 `config.toml` 的 `tools.file_max_*` 传入。
FILE_CAPS = {"max_file_lines": 2_000, "max_file_length": 200_000}
# grep 的单次检索时限、单文件读取上限，以及判断二进制时读入的首段字节数。
GREP_TIMEOUT_SECONDS = 30
GREP_MAX_FILE_BYTES = 1_000_000
GREP_SNIFF_BYTES = 8_192


def _read_text(file: Path) -> Optional[List[str]]:
    """按 UTF-8 读入文本行；二进制、超限或读不了的文件返回 None。"""
    try:
        if file.stat().st_size > GREP_MAX_FILE_BYTES:
            return None
        with open(file, "rb") as handle:
            if b"\0" in handle.read(GREP_SNIFF_BYTES):
                return None
            handle.seek(0)
            return handle.read().decode("utf-8", "ignore").split("\n")
    except OSError:
        return None


class WorkspaceTools(Workspace):
    """Agno Workspace 加两处 aaa 语义：相对路径按启动目录解析；grep 与文件工具共用排除规则。

    依赖 Agno Workspace 的 `_resolve` 与 `_is_excluded`；Agno 升级时由 `tests/test_tools_scope.py` 拦截变化。
    """

    def __init__(self, root: Path, base: Path, grep: bool = False, **kwargs) -> None:
        self.base = Path(base).resolve()
        super().__init__(root=root, **kwargs)
        if grep:
            self.register(self.grep)

    def _resolve(self, path: str, what: str = "path"):
        # 根不是启动目录时（danger-full-access 的根是 /），相对路径仍按启动目录解析。
        if not Path(path).is_absolute() and self.root != self.base:
            path = str(self.base / path)
        return super()._resolve(path, what)

    def _display(self, file: Path) -> str:
        try:
            return file.relative_to(self.base).as_posix()
        except ValueError:
            return str(file)

    def list_files(
        self,
        directory: str = ".",
        pattern: Optional[str] = None,
        recursive: bool = False,
        max_depth: int = 3,
    ) -> str:
        """List entries in a directory; excluded paths are skipped.

        Args:
            directory: Directory to list, relative to the directory aaa was started in (default ".").
            pattern: Optional glob pattern to filter entry names, e.g. '*.py'.
            recursive: If True, walk the tree up to `max_depth` levels (default False).
            max_depth: Depth limit when `recursive=True` (default 3).
        """
        output = super().list_files(directory, pattern, recursive, max_depth)
        if self.root == self.base:
            return output
        try:
            listing = json.loads(output)
        except ValueError:
            return output  # Agno 返回的错误文本
        # Agno 给出的路径相对根；根不是启动目录时换成启动目录相对路径（或绝对路径），才能原样传回其他工具。
        for entry in listing.get("files", []):
            entry["path"] = self._display(self.root / entry["path"])
        return json.dumps(listing, indent=2)

    def _searchable(self, directory: Path, include: Optional[str]) -> Iterator[Path]:
        """逐个产出目录下可检索的文件：跳过被排除的路径与符号链接，可按文件名过滤。

        边走边产出，检索时限才能在 `grep` 里按文件生效；否则巨大目录光列文件就会超时。
        """
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames.sort()
            dirnames[:] = [name for name in dirnames if not self._is_excluded(Path(dirpath) / name)]
            for name in filenames:
                if include and not fnmatch.fnmatch(name, include):
                    continue
                full = Path(dirpath) / name
                if not full.is_symlink() and not self._hidden(full):
                    yield full

    def grep(
        self,
        pattern: str,
        path: Optional[str] = None,
        ignore_case: bool = False,
        include: Optional[str] = None,
        context: int = 0,
        limit: int = 100,
    ) -> str:
        """Search file contents for a regular expression; excluded paths are skipped.

        Args:
            pattern: Regular expression to search for.
            path: Directory or file to search in (default: the directory aaa was started in).
            ignore_case: Case-insensitive search.
            include: Only search files whose name matches this glob, e.g. '*.py'.
            context: Number of context lines to show before and after each match (default 0).
            limit: Maximum number of matching lines returned (default 100).
        """
        if not pattern:
            return "Error: Pattern cannot be empty"
        error, target = self._resolve(path) if path else (None, self.base)
        if error:
            return error
        if not target.exists():
            return f"Error: Path not found: {path or '.'}"
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"Error: Invalid pattern: {exc}"
        context = max(0, int(context or 0))
        limit = max(1, int(limit or 100))
        deadline = time.monotonic() + GREP_TIMEOUT_SECONDS
        files = [target] if target.is_file() else self._searchable(target, include)
        rows: List[str] = []
        matches = 0
        for file in files:
            if matches > limit or time.monotonic() > deadline:
                break
            lines = _read_text(file)
            if lines is None:
                continue
            shown: Dict[int, str] = {}
            for index, line in enumerate(lines):
                if regex.search(line):
                    matches += 1
                    if matches > limit:
                        break
                    for position in range(max(0, index - context), min(len(lines), index + context + 1)):
                        shown.setdefault(position, ":" if position == index else "-")
            for position, separator in sorted(shown.items()):
                rows.append(f"{self._display(file)}{separator}{position + 1}{separator}{lines[position].rstrip()}")
        if time.monotonic() > deadline:
            rows.append(f"[Search stopped after {GREP_TIMEOUT_SECONDS}s]")
        elif matches > limit:
            rows.append(f"[Results limited to {limit} matches]")
        return "\n".join(rows) if rows else f"No matches found for pattern: {pattern}"


def workspace_tool(
    root: Path,
    capabilities: Collection[str],
    file_caps: Optional[Dict[str, int]] = None,
    confirm: Collection[str] = (),
    base: Optional[Path] = None,
) -> WorkspaceTools:
    """按能力清单装配文件与检索工具；未允许的能力不会出现在 schema 里。

    `root` 是文件工具的边界，`base` 是相对路径与 grep 的默认起点（启动目录）。
    `confirm` 是需要人工确认的能力别名（write/edit/move），Agno 据此在执行前
    暂停等 HITL；diff 的计算与询问在 cli 侧。
    """
    allowed = [name for name in FILE_TOOLS if name in capabilities]
    confirmed = [name for name in ("write", "edit", "move") if name in confirm]
    # Workspace 要求 allowed 与 confirm 互斥：进确认名单的不再自动放行。
    allowed = [name for name in allowed if name not in confirmed]
    return WorkspaceTools(
        root=root,
        base=base if base is not None else root,
        allowed=allowed,
        confirm=confirmed,
        grep="grep" in capabilities,
        **(file_caps or FILE_CAPS),
    )
