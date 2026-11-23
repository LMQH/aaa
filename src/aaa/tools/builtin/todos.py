"""任务清单工具：状态存在 run_context.session_state，随会话写库。

规范条目：{"text": str, "phase": str | null, "status": "pending"|"in_progress"|"completed"}。
旧条目只有 text/done，读取时就地归一；写回一律用规范形态。
"""

import json

from agno.run import RunContext

from ...todo_contract import normalize
from ...todo_contract import STATUSES as _STATUSES



def _todos(run_context: RunContext) -> list:
    """取当前会话的任务清单；旧形态在此归一，调用方只见规范条目。"""
    if run_context.session_state is None:
        run_context.session_state = {}
    normalized = normalize(run_context.session_state.get("todos"))
    run_context.session_state["todos"] = normalized
    return normalized


def _reply(todos: list, message: str, ok: bool = True) -> str:
    return json.dumps(
        {"ok": ok, "message": message, "todos": todos}, ensure_ascii=False
    )


def todo_add(run_context: RunContext, items: str, phase: str | None = None) -> str:
    """向当前会话的任务清单追加条目。

    Args:
        items: 一条或多条任务，用换行或分号分隔。
        phase: 可选阶段名，用于分组展示；省略时归入「任务」。
    """
    todos = _todos(run_context)
    added = [part.strip() for part in items.replace(";", "\n").splitlines() if part.strip()]
    group = phase.strip() if isinstance(phase, str) and phase.strip() else None
    for text in added:
        todos.append({"text": text, "phase": group, "status": "pending"})
    return _reply(todos, f"已添加 {len(added)} 条，当前共 {len(todos)} 条。")


def todo_start(run_context: RunContext, index: int) -> str:
    """把任务标记为进行中；同一会话最多一个进行中。

    Args:
        index: 任务编号，从 1 开始（全局编号，不因分组变化）。
    """
    todos = _todos(run_context)
    if not 1 <= index <= len(todos):
        return _reply(todos, f"Error: 编号超出范围（1-{len(todos)}）。", ok=False)
    target = todos[index - 1]
    if target["status"] == "completed":
        return _reply(todos, "Error: 已完成的任务不能重新开始。", ok=False)
    for item in todos:
        if item is not target and item["status"] == "in_progress":
            item["status"] = "pending"
    target["status"] = "in_progress"
    return _reply(todos, f"进行中：{target['text']}")


def todo_list(run_context: RunContext) -> str:
    """列出规范任务清单（含阶段与状态）。"""
    return _reply(_todos(run_context), "任务清单")


def todo_done(run_context: RunContext, index: int) -> str:
    """把任务标记为完成。

    Args:
        index: 任务编号，从 1 开始。
    """
    todos = _todos(run_context)
    if not 1 <= index <= len(todos):
        return _reply(todos, f"Error: 编号超出范围（1-{len(todos)}）。", ok=False)
    target = todos[index - 1]
    if target["status"] == "completed":
        return _reply(todos, f"已完成：{target['text']}")
    target["status"] = "completed"
    return _reply(todos, f"已完成：{target['text']}")
