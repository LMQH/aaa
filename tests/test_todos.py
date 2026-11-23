"""TODO 数据契约：旧清单归一、显式进行中、JSON 回执与全局编号。"""

import json
import unittest
from types import SimpleNamespace

from aaa.tools.builtin.todos import (
    normalize, todo_add, todo_done, todo_list, todo_start,
)


def context(todos=None):
    return SimpleNamespace(session_state={"todos": todos} if todos is not None else {})


def payload(raw):
    data = json.loads(raw)
    return data


class TodoToolsTest(unittest.TestCase):
    def test_legacy_items_normalize_without_inventing_progress(self) -> None:
        ctx = context([{"text": "旧任务", "done": True}, {"text": "未完成"}])
        data = payload(todo_list(ctx))
        self.assertEqual(data["todos"], [
            {"text": "旧任务", "phase": None, "status": "completed"},
            {"text": "未完成", "phase": None, "status": "pending"},
        ])
        self.assertTrue(data["ok"])
        self.assertEqual(
            ctx.session_state["todos"][1],
            {"text": "未完成", "phase": None, "status": "pending"},
        )

    def test_add_with_phase_and_global_numbering(self) -> None:
        ctx = context()
        todo_add(ctx, "A1;A2", "第一阶段")
        todo_add(ctx, "B1", "第二阶段")
        data = payload(todo_start(ctx, 3))
        self.assertTrue(data["ok"])
        self.assertEqual(
            [(t["phase"], t["status"]) for t in data["todos"]],
            [("第一阶段", "pending"), ("第一阶段", "pending"),
             ("第二阶段", "in_progress")],
        )
        # 开始新任务会把旧的进行中退回 pending
        data = payload(todo_start(ctx, 1))
        self.assertEqual([t["status"] for t in data["todos"]],
                         ["in_progress", "pending", "pending"])

    def test_done_is_idempotent_and_rejects_out_of_range(self) -> None:
        ctx = context()
        todo_add(ctx, "唯一任务")
        self.assertTrue(payload(todo_done(ctx, 1))["ok"])
        again = payload(todo_done(ctx, 1))
        self.assertTrue(again["ok"])
        self.assertEqual(again["todos"][0]["status"], "completed")
        bad = payload(todo_done(ctx, 9))
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["message"].startswith("Error:"))
        self.assertEqual(len(bad["todos"]), 1)

    def test_start_completed_or_out_of_range_is_error_without_change(self) -> None:
        ctx = context([{"text": "完成项", "done": True}])
        data = payload(todo_start(ctx, 1))
        self.assertFalse(data["ok"])
        self.assertEqual(data["todos"][0]["status"], "completed")
        missing = payload(todo_start(ctx, 5))
        self.assertFalse(missing["ok"])

    def test_normalize_drops_unknown_shapes(self) -> None:
        self.assertEqual(normalize([None, {}, {"text": " "}, {"text": "ok", "phase": " P "}]), [
            {"text": "ok", "phase": "P", "status": "pending"},
        ])


if __name__ == "__main__":
    unittest.main()
