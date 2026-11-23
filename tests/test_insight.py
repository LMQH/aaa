"""`/insight`：十层采集与只读渲染。计数层用假实现，不触发分词器下载。"""

import contextlib
import io
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import aaa.insight.collect  # noqa: F401  先加载子模块
from aaa.cli import commands as commands_mod
from aaa.cli.commands import handle_command
from aaa.config.settings import Settings
from aaa.insight import tokens as tokens_mod

collect_mod = sys.modules["aaa.insight.collect"]

SEGMENT_ORDER = [
    "基础提示词", "内置工具", "自定义工具", "技能索引", "记忆注入", "压缩摘要",
    "用户消息", "工具调用", "工具结果", "推理", "助手回复",
]


def _fake_state(memory=None):
    messages = [
        SimpleNamespace(role="user", content="问题一"),
        SimpleNamespace(role="assistant", content="回答一", reasoning_content="想一想",
                        tool_calls=[{"function": {"name": "read", "arguments": {"path": "a"}}}]),
        SimpleNamespace(role="tool", content="文件内容"),
        SimpleNamespace(role="user", content="摘要正文", from_history=True),
    ]
    run = SimpleNamespace(messages=messages,
                          metrics={"input_tokens": 100, "output_tokens": 20, "cached_tokens": 50})
    agent = SimpleNamespace(tools=[], db=SimpleNamespace(get_runs=lambda **kwargs: [run]))
    return SimpleNamespace(
        settings=Settings({}), mode="read-only", provider="p", model_id="m",
        catalog=None, runnable=agent, session_id="s", memory=memory, plugin_skills=[],
    )


def _patched_collect(state, tools_count=None, builtin_tools=None):
    """把采集依赖替换成确定性假实现，返回 Insight。"""
    def counter(text, model_id):
        return tokens_mod.CountResult(max(0, len(text) // 4), False)

    def schemas(payloads, model_id):
        return tokens_mod.CountResult(len(payloads) * 10, False)

    with mock.patch.object(collect_mod, "count_text", side_effect=counter), \
            mock.patch.object(collect_mod, "count_tool_schemas",
                              side_effect=tools_count or schemas), \
            mock.patch.object(collect_mod, "system_instructions", return_value="系统提示"), \
            mock.patch.object(collect_mod, "tools_for_mode",
                              return_value=builtin_tools or []):
        return collect_mod.collect(state)


class TokensTest(unittest.TestCase):
    def setUp(self) -> None:
        tokens_mod._cache.clear()

    def test_fallback_when_tokenizer_fails(self) -> None:
        with mock.patch.object(tokens_mod.agno_tokens, "count_text_tokens",
                               side_effect=RuntimeError("no tokenizer")):
            result = tokens_mod.count_text("你好世界", "deepseek-chat")
        self.assertTrue(result.approximate)
        self.assertEqual(result.tokens, (len("你好世界".encode("utf-8")) + 3) // 4)

    def test_prefilter_skips_tokenizer_for_huge_text(self) -> None:
        big = "a" * (tokens_mod._PREFILTER_BYTES + 1)
        with mock.patch.object(tokens_mod.agno_tokens, "count_text_tokens",
                               side_effect=AssertionError("不应调用分词器")):
            result = tokens_mod.count_text(big, "m")
        self.assertTrue(result.approximate)
        self.assertGreater(result.tokens, 0)

    def test_cache_hits_once(self) -> None:
        calls: list[str] = []

        def counter(text, model_id="gpt-4o"):
            calls.append(text)
            return 7

        with mock.patch.object(tokens_mod.agno_tokens, "count_text_tokens", side_effect=counter):
            self.assertEqual(tokens_mod.count_text("same text", "m").tokens, 7)
            self.assertEqual(tokens_mod.count_text("same text", "m").tokens, 7)
        self.assertEqual(len(calls), 1)

    def test_tool_schemas_via_agno(self) -> None:
        with mock.patch.object(tokens_mod.agno_tokens, "count_tool_tokens", return_value=42):
            result = tokens_mod.count_tool_schemas([{"name": "read"}], "m")
        self.assertEqual(result.tokens, 42)
        self.assertFalse(result.approximate)


class CollectTest(unittest.TestCase):
    def test_segments_order_and_usage(self) -> None:
        data = _patched_collect(_fake_state())
        self.assertEqual([seg.name for seg in data.segments], SEGMENT_ORDER)
        by = {seg.name: seg for seg in data.segments}
        self.assertIsNone(by["记忆注入"].count)  # 指针段：不重复计数
        self.assertGreater(by["用户消息"].count.tokens, 0)
        self.assertGreater(by["压缩摘要"].count.tokens, 0)
        self.assertGreater(by["推理"].count.tokens, 0)
        self.assertGreater(by["工具调用"].count.tokens, 0)
        self.assertEqual(data.usage, {"input": 100, "output": 20, "cached": 50})
        self.assertIsNone(data.window)

    def test_builtin_and_custom_tools_split(self) -> None:
        state = _fake_state()
        state.runnable.tools = [
            {"name": "read", "description": "读文件"},
            {"name": "delegate_task", "description": "委派"},
        ]
        data = _patched_collect(state, builtin_tools=[SimpleNamespace(name="read")])
        by = {seg.name: seg for seg in data.segments}
        self.assertEqual(by["内置工具"].count.tokens, 10)
        self.assertEqual(by["自定义工具"].count.tokens, 10)

    def test_memory_enabled_marks_pointer_note(self) -> None:
        host = SimpleNamespace(instructions=lambda text: text)
        state = _fake_state(memory=host)
        with mock.patch.object(collect_mod, "MemoryHost", SimpleNamespace), \
                mock.patch.object(collect_mod, "count_text",
                                  side_effect=lambda text, model_id: tokens_mod.CountResult(0, False)), \
                mock.patch.object(collect_mod, "count_tool_schemas",
                                  side_effect=lambda payloads, model_id: tokens_mod.CountResult(0, False)), \
                mock.patch.object(collect_mod, "system_instructions", return_value="系统提示"), \
                mock.patch.object(collect_mod, "tools_for_mode", return_value=[]):
            data = collect_mod.collect(state)
        by = {seg.name: seg for seg in data.segments}
        self.assertEqual(by["记忆注入"].count, None)
        self.assertIn("召回块", by["记忆注入"].note)


class CommandTest(unittest.TestCase):
    def test_command_renders_table_and_never_forwards(self) -> None:
        state = _fake_state()
        buf = io.StringIO()
        with mock.patch.object(collect_mod, "count_text",
                               side_effect=lambda text, model_id: tokens_mod.CountResult(max(0, len(text) // 4), False)), \
                mock.patch.object(collect_mod, "count_tool_schemas",
                                  side_effect=lambda payloads, model_id: tokens_mod.CountResult(0, False)), \
                mock.patch.object(collect_mod, "system_instructions", return_value="系统提示"), \
                mock.patch.object(collect_mod, "tools_for_mode", return_value=[]), \
                mock.patch.object(commands_mod.insight_mod, "collect",
                                  side_effect=lambda s: _patched_collect(s)), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/insight"))
        output = buf.getvalue()
        self.assertIn("上下文洞察", output)
        self.assertIn("窗口 未知", output)
        self.assertIn("基础提示词", output)
        self.assertIn("合计", output)
        self.assertIn("脚注", output)


if __name__ == "__main__":
    unittest.main()
