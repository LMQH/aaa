"""技能包（目录即包）：递归发现、names-only 索引、`.disabled` 停用与信任指纹。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aaa import plugins, trust
from aaa.cli.commands import handle_command
from aaa.runtime.agent import skills_for_mode


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(root: Path, name: str, desc: str = "说明", *, pack: str | None = None) -> Path:
    base = root / pack if pack else root
    return _write(base / name / "SKILL.md", f"---\nname: {name}\ndescription: {desc}\n---\n正文")


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "skills"

    def test_flat_and_nested_are_both_discovered(self) -> None:
        _skill(self.root, "flat")
        _skill(self.root, "lark-a", pack="lark-cli")
        _skill(self.root, "lark-b", pack="lark-cli")
        entries = plugins._scan_skills([(self.root, "user")])
        names = sorted(e.name for e in entries)
        self.assertEqual(names, ["flat", "lark-a", "lark-b"])
        self.assertEqual([e.pack_name for e in entries if e.name.startswith("lark")],
                         ["lark-cli", "lark-cli"])
        self.assertIsNone(next(e for e in entries if e.name == "flat").pack_name)

    def test_support_dirs_do_not_become_skill_roots(self) -> None:
        skill = _skill(self.root, "alpha")
        _write(skill.parent / "references" / "old" / "SKILL.md", "---\nname: old\n---\n旧包")
        entries = plugins._scan_skills([(self.root, "user")])
        self.assertEqual([e.name for e in entries], ["alpha"])

    def test_dot_dirs_are_skipped(self) -> None:
        _skill(self.root, "keep")
        _skill(self.root / ".archive", "hidden")
        entries = plugins._scan_skills([(self.root, "user")])
        self.assertEqual([e.name for e in entries], ["keep"])

    def test_symlinked_dirs_are_not_followed(self) -> None:
        _skill(self.root, "keep")
        target = self.tmp / "elsewhere"
        _skill(target, "outer")
        (self.root / "link").symlink_to(target, target_is_directory=True)
        entries = plugins._scan_skills([(self.root, "user")])
        self.assertEqual([e.name for e in entries], ["keep"])

    def test_disabled_markers_skip_skill_and_pack(self) -> None:
        _skill(self.root, "lark-a", pack="lark-cli")
        _skill(self.root, "flat")
        _write(self.root / "lark-cli" / ".disabled", "")              # 包级
        _write(self.root / "lark-cli" / "lark-a" / ".disabled", "")   # 技能级
        skills, _ = plugins.build_skills([(self.root, "user")])
        self.assertEqual([s.name for s in skills.get_all_skills()], ["flat"])
        # 技能级标记只影响自己：去掉后整包停用仍然生效。
        (self.root / "lark-cli" / "lark-a" / ".disabled").unlink()
        skills, _ = plugins.build_skills([(self.root, "user")])
        self.assertEqual([s.name for s in skills.get_all_skills()], ["flat"])
        # 包级标记去掉后，包内技能恢复。
        (self.root / "lark-cli" / ".disabled").unlink()
        skills, _ = plugins.build_skills([(self.root, "user")])
        self.assertEqual(sorted(s.name for s in skills.get_all_skills()), ["flat", "lark-a"])


class PackViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "skills"
        _skill(self.root, "lark-a", pack="lark-cli")
        _skill(self.root, "lark-b", pack="lark-cli")
        _skill(self.root, "flat")
        _write(self.root / "lark-cli" / "PACK.md",
               "---\ndescription: 飞书生态技能集，涉及飞书消息与文档时使用。\n---\n")

    def test_pack_info_lists_disabled_pack_with_description(self) -> None:
        _write(self.root / "lark-cli" / ".disabled", "")
        entries = plugins._scan_skills([(self.root, "user")])
        infos = plugins.pack_infos(entries)
        self.assertEqual([(p.name, p.enabled, p.members) for p in infos],
                         [("lark-cli", False, ("lark-a", "lark-b"))])
        self.assertIn("飞书生态", infos[0].description)
        self.assertEqual(infos[0].path, self.root / "lark-cli")

    def test_names_only_index_keeps_member_names(self) -> None:
        _skill(self.root, "lark-a", "飞书技能说明", pack="lark-cli")
        skills, _ = plugins.build_skills([(self.root, "user")])
        snippet = skills.get_system_prompt_snippet()
        self.assertIn("<skillpack>", snippet)
        self.assertIn("飞书生态技能集", snippet)
        self.assertIn("lark-a, lark-b", snippet)
        # 描述省了，名字在：包内技能自己的 <skill> 条目不再出现。
        self.assertNotIn("飞书技能说明", snippet)
        self.assertIn("get_skill_instructions(成员名)", snippet)
        import json

        self.assertEqual(
            json.loads(skills._get_skill_instructions("lark-a"))["instructions"], "正文")

    def test_flat_skills_keep_full_entries(self) -> None:
        skills, _ = plugins.build_skills([(self.root, "user")])
        snippet = skills.get_system_prompt_snippet()
        head = snippet.split("<skillpack>")[0]
        self.assertIn("<description>说明</description>", head)

    def test_skill_packs_map_only_counts_enabled(self) -> None:
        _write(self.root / "lark-cli" / ".disabled", "")
        found = plugins.discover_extensions(None, skills_user_dirs=[self.root])
        self.assertNotIn("lark-a", found.skill_packs)
        self.assertNotIn("lark-b", found.skill_packs)
        self.assertEqual([(p.name, p.enabled) for p in found.pack_info],
                         [("lark-cli", False)])

    def test_enabling_pack_restores_members(self) -> None:
        _write(self.root / "lark-cli" / ".disabled", "")
        (self.root / "lark-cli" / ".disabled").unlink()
        found = plugins.discover_extensions(None, skills_user_dirs=[self.root])
        self.assertEqual(found.skill_packs.get("lark-a"), "lark-cli")
        self.assertEqual(found.skill_packs.get("lark-b"), "lark-cli")
        names = sorted(s.name for s in found.skills.get_all_skills())
        self.assertIn("lark-a", names)

    def test_precedence_unchanged_by_packs(self) -> None:
        project = self.tmp / "project"
        _skill(project, "lark-a", "项目技能", pack="lark-cli")
        skills, sources = plugins.build_skills([(project, "project"), (self.root, "user")])
        self.assertEqual(skills.get_skill("lark-a").description, "项目技能")
        self.assertEqual(sources["lark-a"], "project")
        self.assertIsNone(skills.get_skill("archify"))

    def test_flat_winner_is_untagged_across_roots(self) -> None:
        """QA D1：同名技能「平铺赢家 + 包内输家」时，赢家按无包渲染、不打包标签。"""
        project = self.tmp / "project"
        _skill(project / ".aaa" / "skills", "dup", "项目平铺版")
        _skill(self.root, "dup", "用户包内版", pack="pk2")
        _write(self.root / "pk2" / "PACK.md", "---\ndescription: 包二\n---\n")
        found = plugins.discover_extensions(
            plugins.project_dirs(project), trusted=frozenset({"skills"}),
            skills_user_dirs=[self.root],
        )
        self.assertEqual(found.skill_packs.get("dup"), "")
        snippet = found.skills.get_system_prompt_snippet()
        self.assertIn("项目平铺版", snippet)
        self.assertNotIn("用户包内版", snippet)


class ReadOnlyModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "skills"
        _skill(self.root, "lark-a", pack="lark-cli")
        _write(self.root / "lark-cli" / "PACK.md", "---\ndescription: 飞书\n---\n")

    def test_without_scripts_keeps_pack_view_and_drops_tool(self) -> None:
        skills, _ = plugins.build_skills([(self.root, "user")])
        readonly = skills_for_mode(skills, "read-only")
        self.assertNotIn("get_skill_script", [t.name for t in readonly.get_tools()])
        self.assertIn("<skillpack>", readonly.get_system_prompt_snippet())
        self.assertIn("lark-a", readonly.get_system_prompt_snippet())

    def test_write_mode_keeps_script_tool(self) -> None:
        skills, _ = plugins.build_skills([(self.root, "user")])
        kept = skills_for_mode(skills, "workspace-write")
        self.assertIn("get_skill_script", [t.name for t in kept.get_tools()])


class TrustFingerprintTest(unittest.TestCase):
    def test_nested_files_and_pack_doc_are_fingerprinted(self) -> None:
        root = self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())) / "skills"
        _skill(root, "lark-a", pack="lark-cli")
        _write(root / "lark-cli" / "PACK.md", "---\ndescription: 飞书\n---\n")
        _write(root / "lark-cli" / "lark-a" / "scripts" / "run.sh", "echo hi\n")
        files = trust._kind_files({"skills": root}, "skills")
        names = [p.name for p in files]
        self.assertIn("SKILL.md", names)
        self.assertIn("PACK.md", names)
        self.assertIn("run.sh", names)

    def test_files_outside_skill_dirs_are_not_fingerprinted(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "skills"
        _skill(root, "lark-a", pack="lark-cli")
        _write(root / "loose.md", "无关文件")
        files = trust._kind_files({"skills": root}, "skills")
        self.assertNotIn("loose.md", [p.name for p in files])


class CliTest(unittest.TestCase):
    def _run(self, state, text: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handle_command(state, text)
        return buf.getvalue()

    def test_plugin_toggles_pack_marker(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "skills"
        _skill(root, "lark-a", pack="lark-cli")
        _write(root / "lark-cli" / "PACK.md", "---\ndescription: 飞书\n---\n")
        found = plugins.discover_extensions(None, skills_user_dirs=[root])
        state = SimpleNamespace(plugin_extensions=[], skill_pack_info=found.pack_info)
        out = self._run(state, "/plugin lark-cli")
        self.assertIn("技能包 lark-cli 已停用", out)
        self.assertTrue((root / "lark-cli" / ".disabled").exists())
        self.assertEqual([p.enabled for p in plugins.pack_infos(plugins._scan_skills([(root, "user")]))],
                         [False])
        out = self._run(state, "/plugin lark-cli")
        self.assertIn("技能包 lark-cli 已启用", out)
        self.assertFalse((root / "lark-cli" / ".disabled").exists())

    def test_skills_shows_pack_tag(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "skills"
        _skill(root, "lark-a", pack="lark-cli")
        found = plugins.discover_extensions(None, skills_user_dirs=[root])
        state = SimpleNamespace(
            plugin_skills=found.skills,
            skill_sources=found.skill_sources,
            skill_packs=found.skill_packs,
        )
        out = self._run(state, "/skills")
        self.assertIn("lark-a [用户] [lark-cli]", out)


if __name__ == "__main__":
    unittest.main()
