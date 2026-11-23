"""斜杠命令的使用频次账本：本地小 JSON，排序补全与 /help。

损坏或缺失一律按空账本处理，回退字母序；写入失败静默忽略，不影响命令执行。
"""

import json
import os

from ..config.paths import COMMAND_USAGE_FILE


def read_counts() -> dict[str, int]:
    try:
        raw = json.loads(COMMAND_USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int) and v >= 0}


def note_used(name: str) -> None:
    """记一次使用；name 不含斜杠（如 "memory"）。写失败不打扰。"""
    counts = read_counts()
    counts[name] = counts.get(name, 0) + 1
    tmp = COMMAND_USAGE_FILE.with_suffix(".json.tmp")
    try:
        COMMAND_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(counts, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, COMMAND_USAGE_FILE)
    except OSError:
        pass


def ordered(names: list[str]) -> list[str]:
    """次数降序优先，同次数按字母 A→Z；names 允许带斜杠前缀。"""
    counts = read_counts()
    return sorted(names, key=lambda n: (-counts.get(n.lstrip("/"), 0), n))
