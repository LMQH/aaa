"""把 Agno 与本程序的日志落到 ~/.aaa/logs/aaa.log。"""

import logging
import os
from typing import Any, Protocol

from ..config.paths import DB_FILE, LOG_DIR, LOG_FILE

# 进程日志级别：config.toml 的 log.level，AAA_LOG_LEVEL 可临时覆盖。
ENV_LEVEL = "AAA_LOG_LEVEL"


class SettingsLike(Protocol):
    def get(self, path: str) -> Any: ...


def setup_logging(settings: SettingsLike) -> None:
    """把 Agno 与本程序的日志追加到 ~/.aaa/logs/aaa.log。

    Agno 自带 logger（名字 "agno"），但只输出到终端；这里给它挂一个文件
    handler，不引入额外依赖，也不改变原有行为。重复调用不会重复挂载。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 日志轮转：超过上限时归档，保留两份。
    max_bytes = int(settings.get("log.max_bytes"))
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > max_bytes:
        for index in (2, 1):
            source = LOG_FILE.with_name(LOG_FILE.name + f".{index}")
            if source.exists():
                source.replace(LOG_FILE.with_name(LOG_FILE.name + f".{index + 1}"))
        LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))

    # 数据库只增不减；过大时提示，不自动删除用户数据。
    if DB_FILE.exists() and DB_FILE.stat().st_size > int(settings.get("log.db_warn_bytes")):
        logging.getLogger("aaa").warning("sessions.db 已超过配置的告警阈值，请手动归档。")

    if any(getattr(handler, "_agno_assistant_file", False) for handler in logging.getLogger("agno").handlers):
        return
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler._agno_assistant_file = True  # type: ignore[attr-defined]
    level_name = (os.getenv(ENV_LEVEL) or str(settings.get("log.level"))).upper()
    level = getattr(logging, level_name, logging.INFO)
    for name in ("agno", "aaa"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        # Agno 默认带终端 handler；非交互 JSON 输出必须是纯 JSONL，统一只落文件。
        logger.handlers = [
            existing for existing in logger.handlers
            if getattr(existing, "_agno_assistant_file", False)
        ]
        logger.propagate = False
        logger.addHandler(handler)
