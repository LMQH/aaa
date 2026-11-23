"""文件与检索类工具：由 Agno 提供，这里只按能力清单装配。

能力名（read / list / write / edit / move / delete / grep）由 `boundary.policy` 决定。
"""

from pathlib import Path
from typing import Collection, Dict, Optional

from agno.tools.coding import CodingTools
from agno.tools.workspace import Workspace

# Workspace 能提供的能力；顺序即装配顺序。
FILE_TOOLS = ("read", "list", "write", "edit", "move", "delete")
# 单文件读取上限的默认值；实际值由 `config.toml` 的 tools.file_max_* 传入。
FILE_CAPS = {"max_file_lines": 2_000, "max_file_length": 200_000}


def workspace_tool(
    root: Path,
    capabilities: Collection[str],
    file_caps: Optional[Dict[str, int]] = None,
    confirm: Collection[str] = (),
) -> Workspace:
    """按能力清单装配 Workspace；未允许的能力不会出现在 schema 里。

    `confirm` 是需要人工确认的能力别名（write/edit/move），Agno 据此在执行前
    暂停等 HITL；diff 的计算与询问在 cli 侧。
    """
    allowed = [name for name in FILE_TOOLS if name in capabilities]
    confirmed = [name for name in ("write", "edit", "move") if name in confirm]
    # Workspace 要求 allowed 与 confirm 互斥：进确认名单的不再自动放行。
    allowed = [name for name in allowed if name not in confirmed]
    return Workspace(root=root, allowed=allowed, confirm=confirmed, **(file_caps or FILE_CAPS))


def grep_tool(root: Path) -> CodingTools:
    """Agno 原生 grep（Pi 风格）：正则、行号、`-C` 上下文、`--include`。

    只开 grep，其余能力由 Workspace 与 ProcessTools 承担，避免同义入口。
    """
    return CodingTools(
        base_dir=root,
        restrict_to_base_dir=True,
        enable_read_file=False,
        enable_edit_file=False,
        enable_write_file=False,
        enable_run_shell=False,
        enable_grep=True,
        add_instructions=False,
    )
