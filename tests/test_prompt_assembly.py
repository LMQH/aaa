"""系统提示词装配：工具指引只随实际装配的工具出现，顺序固定。"""

import os
import unittest
from unittest.mock import patch

from aaa.runtime import prompt as prompt_mod

READ_ONLY = ("read_file", "list_files", "grep", "todo_add", "todo_start", "todo_done", "todo_list")
WRITE = READ_ONLY + ("write_file", "edit_file", "move_file", "run_shell_command", "job_output", "job_kill")
FULL = WRITE + ("delete_file",)
DELEGATION = ("list_delegations", "register_delegation", "delegate_task", "save_delegation")
GUIDED = {
    "read_file", "list_files", "grep", "write_file", "edit_file", "move_file", "delete_file",
    "todo_add", "todo_start", "todo_done", "todo_list",
    "list_delegations", "delegate_task", "save_delegation",
}


def bullets(text):
    """正文里以工具名开头的条目所对应的工具名。"""
    return {line.split("`")[1] for line in text.split("\n") if line.startswith("- `")} & GUIDED


class PromptAssemblyTests(unittest.TestCase):
    def test_read_only_has_no_write_shell_or_delegation_guidance(self):
        text = prompt_mod.instructions_for("read-only", READ_ONLY)
        self.assertTrue(bullets(text) & {"read_file", "list_files", "grep"})
        self.assertFalse(bullets(text) & {"write_file", "edit_file", "move_file", "delete_file"})
        for absent in ("run_shell_command", "job_output", "delegate_task"):
            self.assertNotIn(f"`{absent}`", text)

    def test_full_access_brings_delete_guidance(self):
        text = prompt_mod.instructions_for("danger-full-access", FULL)
        self.assertIn("`delete_file`", text)
        self.assertIn("<permission_mode>danger-full-access</permission_mode>", text)

    def test_sections_follow_assembled_tools(self):
        text = prompt_mod.instructions_for("workspace-write", WRITE + DELEGATION)
        self.assertEqual(
            bullets(text),
            {"read_file", "list_files", "grep", "write_file", "edit_file", "move_file",
             "todo_add", "todo_start", "todo_done", "todo_list", "list_delegations",
             "delegate_task", "save_delegation"},
        )
        self.assertNotIn("`delete_file`", text)
        self.assertIn("`run_shell_command`", text)
        self.assertIn("`job_output`", text)

    def test_one_file_tool_still_brings_only_its_own_bullet(self):
        text = prompt_mod.instructions_for("workspace-write", ("write_file",))
        self.assertEqual(bullets(text), {"write_file"})

    def test_order_is_stable(self):
        text = prompt_mod.instructions_for(
            "workspace-write", WRITE + DELEGATION, "<project_instructions>规则</project_instructions>"
        )
        positions = [
            text.index("<project_instructions>规则</project_instructions>"),
            text.index("<cwd>"),
            text.index("<permission_mode>"),
            text.index("`run_shell_command`"),
            text.index("`write_file`"),
            text.index("`job_output`"),
            text.index("`todo_add`"),
            text.index("`delegate_task`"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions[0], positions[1] - len("<project_instructions>规则</project_instructions>\n\n"))

    def test_cwd_is_the_working_directory(self):
        text = prompt_mod.instructions_for("read-only", READ_ONLY)
        self.assertIn(f"<cwd>{os.getcwd()}</cwd>", text)

    @patch.object(prompt_mod, "SYSTEM_PROMPT_FILE")
    def test_user_system_file_replaces_only_the_base(self, system_file):
        system_file.read_text.return_value = "自定义基础指令\n"
        text = prompt_mod.instructions_for("read-only", READ_ONLY)
        self.assertTrue(text.startswith("自定义基础指令"))
        self.assertIn("<permission_mode>read-only</permission_mode>", text)
        self.assertIn("`read_file`", text)


if __name__ == "__main__":
    unittest.main()
