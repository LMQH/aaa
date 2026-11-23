"""aaa 外置记忆引擎适配：入口点指向这里的 `ENGINE`。"""

from __future__ import annotations

from pathlib import Path

from .engine import Engine


def ENGINE(data_dir: Path) -> Engine:
    """宿主只传独立数据目录；其余配置、模型与存储由本引擎自管。"""
    return Engine(Path(data_dir))
