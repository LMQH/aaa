"""CLI 只读查看 Agno 内置记忆；不是插件，也不参与外置装配。"""

from agno.db.sqlite import SqliteDb
from agno.memory import UserMemory


def read_builtin(
    db: SqliteDb, user_id: str, *, limit: int, page: int
) -> tuple[list[UserMemory], int]:
    if not user_id:
        raise ValueError("读取记忆必须限定 user_id")
    rows, total = db.get_user_memories(
        user_id=user_id,
        limit=limit,
        page=page,
        sort_by="updated_at",
        sort_order="desc",
        deserialize=False,
    )
    return [UserMemory.from_dict(row) for row in rows], total
