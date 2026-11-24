"""会话分叉：回到某一轮之前换个问法。

Agno 的会话按轮次列表线性回放，没有「当前路径」。做法是把当前会话整份复制成新会话，
再在副本里删掉目标轮次及其之后的轮次；原会话不动。只用 Agno 的公开接口。
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import List, Optional

from agno.run.base import RunStatus


@dataclass(frozen=True)
class Turn:
    """一轮已完成、带用户输入的运行；`number` 从 1 起，按轮次顺序。"""

    number: int
    run_id: str
    text: str


def _ordered_runs(runnable, session_id: str) -> list:
    return runnable.db.get_runs(session_id=session_id, sort_by="run_index", sort_order="asc") or []


def _user_text(run) -> Optional[str]:
    """这一轮的用户原话：取消息里最后一条纯文本的 user 消息，没有再用 `run.input`。"""
    for message in reversed(getattr(run, "messages", None) or []):
        content = getattr(message, "content", None)
        if getattr(message, "role", None) == "user" and isinstance(content, str) and content.strip():
            return content
    raw = getattr(run, "input", None)
    return raw if isinstance(raw, str) and raw.strip() else None


def user_turns(runnable, session_id: str) -> List[Turn]:
    """会话里已完成、带用户输入的轮次，按轮次顺序编号。"""
    turns: List[Turn] = []
    for run in _ordered_runs(runnable, session_id):
        text = _user_text(run) if getattr(run, "status", None) == RunStatus.completed else None
        if text is not None:
            turns.append(Turn(len(turns) + 1, run.run_id, text))
    return turns


def fork_before(runnable, session_id: str, run_id: str, user_id: Optional[str] = None) -> str:
    """把会话复制成新会话，新会话只保留 `run_id` 之前的轮次；返回新会话 id。"""
    runs = _ordered_runs(runnable, session_id)
    index = next((position for position, run in enumerate(runs) if run.run_id == run_id), None)
    if index is None:
        raise ValueError("找不到这一轮：会话内容可能已经变化，请重试。")
    new_id = runnable.fork_session(source_session_id=session_id, user_id=user_id)
    copies = _ordered_runs(runnable, new_id)
    if len(copies) != len(runs):
        runnable.db.delete_session(session_id=new_id)
        raise RuntimeError("分叉副本与原会话的轮次数不一致，已放弃本次分叉。")
    tail = [run.run_id for run in copies[index:]]
    if tail:
        runnable.db.delete_runs(run_ids=tail)
    _restore_state(runnable.db, new_id, copies[:index])
    return new_id


def _restore_state(db, session_id: str, kept: list) -> None:
    """副本的会话状态回到最后一个保留轮次结束时，不带入之后轮次的状态。"""
    session = db.get_session(session_id, runs_limit=0)
    if session is None:
        return
    session.session_data = dict(session.session_data or {})
    state = getattr(kept[-1], "session_state", None) if kept else None
    if state:
        session.session_data["session_state"] = deepcopy(state)
    else:
        session.session_data.pop("session_state", None)
    db.upsert_session(session)
