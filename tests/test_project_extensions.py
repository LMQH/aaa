"""项目级扩展（批 2）：发现顺序、来源标注、AGENTS.md 注入与配置键。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aaa import plugins
from aaa.cli.commands import handle_command
from aaa.config.settings import Settings
from aaa.runtime import prompt as prompt_mod


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ProjectRootTest(unittest.TestCase):
    def test_walks_up_to_git_root(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".git").mkdir()
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(plugins.project_root(nested), root.resolve())

    def test_without_git_uses_start(self) -> None:
        start = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(plugins.project_root(start), start.resolve())


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = self.root / ".aaa"
        self.user = self.root / "user"

    def test_commands_label_source(self) -> None:
        _write(self.project / "commands" / "brief.md", "项目版")
        _write(self.user / "commands" / "brief.md", "用户版")
        found = plugins.discover_commands([
            (self.project / "commands", "project"),
            (self.user / "commands", "user"),
        ])
        self.assertEqual([(c.name, c.source) for c in found],
                         [("brief", "project"), ("brief", "user")])

    def test_state_rule_first_wins(self) -> None:
        _write(self.project / "commands" / "brief.md", "项目版")
        _write(self.user / "commands" / "brief.md", "用户版")
        commands: dict = {}
        for command in plugins.discover_commands([
            (self.project / "commands", "project"),
            (self.user / "commands", "user"),
        ]):
            commands.setdefault(command.name, command)
        self.assertEqual(commands["brief"].source, "project")
        self.assertEqual(commands["brief"].template, "项目版")

    def test_skills_dedupe_keeps_project(self) -> None:
        _write(self.project / "skills" / "brief" / "SKILL.md",
               "---\nname: brief\ndescription: 项目技能\n---\nP")
        _write(self.user / "skills" / "brief" / "SKILL.md",
               "---\nname: brief\ndescription: 用户技能\n---\nU")
        found = plugins.discover_skills([
            (self.project / "skills", "project"),
            (self.user / "skills", "user"),
        ])
        self.assertEqual([(s.name, s.source, s.description) for s in found],
                         [("brief", "project", "项目技能")])


class ProjectInstructionsTest(unittest.TestCase):
    def _patch(self, root: Path):
        return mock.patch.object(
            prompt_mod, "project_dirs",
            return_value={"root": root, "agents_md": root / "AGENTS.md"},
        )

    def test_injected_before_permission_mode(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".git").mkdir()
        _write(root / "AGENTS.md", "项目规则：只读验证")
        with self._patch(root):
            block = prompt_mod.project_instructions(8000)
            text = prompt_mod.instructions_for("read-only", [], block)
        self.assertIn("项目规则：只读验证", text)
        self.assertLess(text.index("项目规则"), text.index("<permission_mode>"))

    def test_cap_and_disabled(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".git").mkdir()
        _write(root / "AGENTS.md", "长" * 200)
        with self._patch(root):
            self.assertEqual(prompt_mod.project_instructions(0), "")
            capped = prompt_mod.project_instructions(40)
        self.assertIn("已截断", capped)
        self.assertLess(len(capped), 120)

    def test_missing_file_is_empty(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self._patch(root):
            self.assertEqual(prompt_mod.project_instructions(8000), "")


class SettingsKeysTest(unittest.TestCase):
    def test_project_defaults(self) -> None:
        settings = Settings({})
        self.assertTrue(settings.get("project.enabled"))
        self.assertEqual(settings.get("project.instructions_max_chars"), 8000)


class SkillsLabelTest(unittest.TestCase):
    def test_command_marks_sources(self) -> None:
        state = SimpleNamespace(plugin_skills=[
            plugins.Skill("alpha", Path("/tmp/a/SKILL.md"), "项目技能", "project"),
            plugins.Skill("beta", Path("/tmp/b/SKILL.md"), "内置技能", "builtin"),
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/skills"))
        output = buf.getvalue()
        self.assertIn("[项目]", output)
        self.assertIn("[内置]", output)


if __name__ == "__main__":
    unittest.main()
