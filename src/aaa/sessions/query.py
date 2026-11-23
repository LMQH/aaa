"""会话查询、命名与标题生成（Agno SqliteDb 的薄封装）。

条数上限、会话 id 长度与标题截断长度来自配置，由调用方传入 `settings`。
"""

import uuid
from datetime import datetime
from typing import Any, Optional, Protocol, Set

from agno.agent import Agent
from agno.session import AgentSession
from agno.db.base import SessionType
from agno.models.message import Message

from ..config.paths import DB_FILE
from ..model.provider import Channel, ChannelModel, build_title_model
from . import index


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...


def new_session_id(settings: SettingsLike) -> str:
    length = int(settings.get("sessions.id_length"))
    return uuid.uuid4().hex[:length]


def list_sessions(runnable: Agent, limit: int) -> list[AgentSession]:
    """按更新时间列出 Agent 会话。"""
    return runnable.db.get_sessions(
        session_type=SessionType.AGENT, limit=limit,
        sort_by="updated_at", sort_order="desc", include_runs=False,
    ) or []


def remember_session(cwd: str, session_id: str) -> None:
    """记下某目录最近使用的会话，供下次 `-c` 续接（落库内索引表）。"""
    index.remember(DB_FILE, cwd, session_id)


def latest_session_for_cwd(runnable: Agent, cwd: str, settings: SettingsLike) -> AgentSession | None:
    """当前目录最近的会话：先查询索引，再按目录匹配历史。"""
    session_id = index.latest(DB_FILE, cwd)
    if session_id:
        # Agno 的 session_type 参数决定反序列化类型，不过滤实际存储类型。
        session = runnable.db.get_session(session_id, runs_limit=0)
        if isinstance(session, AgentSession):
            return session
    for session in list_sessions(runnable, limit=int(settings.get("sessions.search_limit"))):
        if (session.metadata or {}).get("cwd") == cwd:
            return session
    return None


def find_session(runnable: Agent, prefix: str, settings: SettingsLike) -> AgentSession | None:
    """按完整 id 或前缀找会话；多个前缀命中时取最近的一个。"""
    if not prefix:
        return None
    session = runnable.db.get_session(prefix, runs_limit=0)
    if session is not None:
        return session if isinstance(session, AgentSession) else None
    for session in list_sessions(runnable, limit=int(settings.get("sessions.search_limit"))):
        if session.session_id.startswith(prefix):
            return session
    return None


def session_label(session: AgentSession) -> str:
    """列表中的一行：标题 · id · 更新时间。"""
    data = session.session_data or {}
    title = data.get("session_name") or "(未命名)"
    when = datetime.fromtimestamp(session.updated_at).strftime("%m-%d %H:%M") if session.updated_at else ""
    return f"{title}  ·  {session.session_id}  ·  {when}"


TITLE_PROMPT = (
    "Generate a session title. Output only: TAG: summary\n"
    "TAG is 3-5 uppercase ASCII letters; summary is at most 12 words. "
    "No emoji, no quotes.\n"
    "Examples:\n"
    "HARN: Design a minimal harness\n"
    "DEPLOY: Finish refactor and deploy"
)


def generate_title(channel: Channel, model: ChannelModel, text: str, max_length: int) -> str:
    """按 omp 的格式让模型生成标题；失败返回空串。"""
    try:
        client = build_title_model(channel, model)
        response = client.response([
            Message(role="system", content=TITLE_PROMPT),
            Message(role="user", content=" ".join(text.split())[:500]),
        ])
        title = (response.content or "").strip().splitlines()[0].strip().strip('"')
        while title and not title[0].isascii():
            title = title[1:]
        return title.strip()[:max_length]
    except Exception:
        return ""


def ensure_session_name(
    agent: Agent,
    channel: Channel,
    model: ChannelModel,
    session_id: str,
    text: str,
    named: Set[str],
    title_prefix: str,
    settings: SettingsLike,
) -> None:
    """首次发言后按 omp 格式生成标题；已有名字则跳过。"""
    if session_id in named:
        return
    try:
        if not agent.get_session_name(session_id=session_id):
            title = generate_title(
                channel, model, text, int(settings.get("sessions.title_length"))
            )
            if not title:
                return
            agent.db.rename_session(session_id, SessionType.AGENT, f"{title_prefix} {title}")
    except Exception:
        return
    named.add(session_id)
