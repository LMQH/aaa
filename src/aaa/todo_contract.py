"""TODO 条目契约：旧 {text,done} 与规范条目的唯一归一实现。

无包内依赖：工具层与 CLI 共用同一份定义，避免两边各自解释字段。
"""

STATUSES = ("pending", "in_progress", "completed")


def normalize(items) -> list:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = item.get("status")
        if status not in STATUSES:
            status = "completed" if item.get("done") else "pending"
        phase = item.get("phase")
        out.append({
            "text": text,
            "phase": str(phase).strip() if isinstance(phase, str) and phase.strip() else None,
            "status": status,
        })
    return out
