"""落盘前的改动预览：write/edit/move 的统一 diff。

diff 在 cli 侧计算——读旧内容、按工具参数模拟结果，工具本身不执行。
算不出来（文件缺失、`old_str` 未命中、参数异常）返回 None，询问退化为
普通确认（只报工具名与参数），不猜测改动内容。
"""

import difflib
from pathlib import Path
from typing import Dict, Optional

from ..boundary.policy import REVIEW_FILE_TOOLS

# diff 超长时截断；预览是给人看的，不必完整展开巨型文件。
MAX_DIFF_LINES = 200


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _unified(rel: str, old: str, new: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}",
    ))
    if len(lines) > MAX_DIFF_LINES:
        return "".join(lines[:MAX_DIFF_LINES]) + f"\n…（diff 共 {len(lines)} 行，已截断）"
    return "".join(lines)


def change_diff(name: str, args, root) -> Optional[str]:
    """按工具参数模拟改动，返回给人看的预览文本；无法预览时返回 None。"""
    if name not in REVIEW_FILE_TOOLS or not isinstance(args, dict):
        return None
    base = Path(root)

    if name == "move_file":
        dst = args.get("dst")
        src = args.get("src")
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            return None
        # 与 edit 同一原则：工具会失败的调用不预览假改动。
        if not (base / src).exists():
            return None
        note = "（覆盖已有目标）" if args.get("overwrite") else ""
        return f"移动 {src} → {dst}{note}"

    rel = args.get("path")
    if not isinstance(rel, str) or not rel:
        return None

    if name == "write_file":
        target = base / rel
        old = _read(target)
        content = args.get("content")
        if not isinstance(content, str):
            return None
        if old is None:
            # 存在但读不出来：不知道会覆盖什么，不预览。
            if target.exists():
                return None
            return f"新建 {rel}（{len(content)} 字节）"
        return _unified(rel, old, content)

    if name == "edit_file":
        target = base / rel
        old = _read(target)
        old_str, new_str = args.get("old_str"), args.get("new_str")
        if old is None or not isinstance(old_str, str) or not isinstance(new_str, str):
            return None
        count = old.count(old_str)
        if count == 0:
            return None
        if count > 1 and not args.get("replace_all"):
            # 与工具行为一致：多处命中且未开 replace_all 会失败，不预览假改动。
            return None
        new = old.replace(old_str, new_str) if args.get("replace_all") else old.replace(old_str, new_str, 1)
        return _unified(rel, old, new)

    return None
