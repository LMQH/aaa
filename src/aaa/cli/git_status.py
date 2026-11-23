"""Git 状态：只读分支与文件计数，异步读取，失败不外抛。"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitStatus:
    branch: str
    changed: int
    untracked: int

    def display(self) -> str:
        flag = f"*{self.changed}" if self.changed else ""
        extra = f" ?{self.untracked}" if self.untracked else ""
        return f"⑂ {self.branch}{(' ' + flag) if flag else ''}{extra}"


def _parse(data: bytes) -> Optional[GitStatus]:
    if not data:
        return None
    branch = ""
    changed = untracked = 0
    fields = [entry for entry in data.split(b"\x00")]
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if entry.startswith(b"## "):
            head = entry[3:].decode("utf-8", "replace")
            branch = head.split("...")[0].strip()
            if branch.startswith("No commits yet on "):
                branch = branch[len("No commits yet on "):]
            if branch.startswith("HEAD (no branch)"):
                branch = "detached"
            continue
        status = entry[:2]
        if status == b"??":
            untracked += 1
        elif len(status) == 2:
            changed += 1
            # rename/copy 的下一字段是原路径，不是独立条目
            if status[:1] in (b"R", b"C"):
                index += 1
    if not branch:
        return None
    return GitStatus(branch=branch or "detached", changed=changed, untracked=untracked)


async def read_status(cwd: Path, timeout: float) -> Optional[GitStatus]:
    """只读 `git status --porcelain=v1 -z --branch`；非仓库/超时返回 None。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain=v1", "-z", "--branch",
            cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return _parse(out)
    except asyncio.TimeoutError:
        logger.debug("git status timed out")
        try:
            process.kill()
            await process.wait()
        except (UnboundLocalError, ProcessLookupError):
            pass
        return None
    except OSError:
        logger.debug("git status unavailable", exc_info=True)
        return None
