"""项目级代码扩展的信任门：指纹、确认、撤销与加载隔离。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aaa import plugins
from aaa import trust as trust_mod
from aaa.cli import animations
from aaa.cli.commands import handle_command
from aaa.config.settings import Settings
from aaa.memory import registry as registry_mod


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TrustBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".git").mkdir()
        self.project = plugins.project_dirs(self.root)
        self.store = self.root / "repos.json"
        patcher = mock.patch.object(trust_mod, "TRUST_FILE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)


class FingerprintTest(TrustBase):
    def test_change_detection_and_revoke(self) -> None:
        anim = self.project["animations"] / "sharingan.py"
        _write(anim, "ANIMATION = None\n")
        self.assertTrue(trust_mod.fingerprint(self.project, "animations"))
        self.assertIn("animations", trust_mod.pending_kinds(self.project))

        trust_mod.approve(self.project, ["animations"])
        self.assertIn("animations", trust_mod.trusted_kinds(self.project))
        self.assertNotIn("animations", trust_mod.pending_kinds(self.project))

        _write(anim, "ANIMATION = 'changed'\n")
        self.assertIn("animations", trust_mod.pending_kinds(self.project))
        self.assertNotIn("animations", trust_mod.trusted_kinds(self.project))

        self.assertTrue(trust_mod.revoke(str(self.project["root"])))
        self.assertEqual(trust_mod.entries(), {})


class EnsureTest(TrustBase):
    def test_non_interactive_never_asks(self) -> None:
        _write(self.project["memory_engines"] / "proj.py", "ENGINE = lambda data_dir: None\n")

        def ask(kinds, text):
            raise AssertionError("非交互模式不应询问")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            trusted = trust_mod.ensure(self.project, interactive=False, ask=ask)
        self.assertEqual(trusted, set())
        self.assertIn("--trust-repo", err.getvalue())
        self.assertEqual(trust_mod.entries(), {})

    def test_interactive_asks_once_for_all_pending_kinds(self) -> None:
        _write(self.project["extensions"] / "toy" / "extension.toml", '[mcp]\ncommand = "x"\n')
        _write(self.project["animations"] / "glow.py", "ANIMATION = None\n")
        seen = []

        def ask(kinds, text):
            seen.append((list(kinds), text))
            return "once"

        trusted = trust_mod.ensure(self.project, interactive=True, ask=ask)
        self.assertEqual(len(seen), 1)
        self.assertEqual(set(seen[0][0]), {"extensions", "animations"})
        self.assertIn("启动动画", seen[0][1])
        self.assertIn("MCP 扩展", seen[0][1])
        self.assertEqual(trusted, {"extensions", "animations"})
        self.assertIn("extensions", trust_mod.trusted_kinds(self.project))

    def test_always_policy_survives_content_changes(self) -> None:
        anim = _write(self.project["animations"] / "glow.py", "ANIMATION = None\n")
        trust_mod.ensure(self.project, interactive=True, ask=lambda kinds, text: "always")
        _write(anim, "ANIMATION = 'changed'\n")
        self.assertIn("animations", trust_mod.trusted_kinds(self.project))
        self.assertEqual(trust_mod.pending_kinds(self.project), [])
        self.assertEqual(trust_mod.status(self.project, "animations"), "always")

    def test_deny_is_remembered_until_content_changes(self) -> None:
        anim = _write(self.project["animations"] / "evil.py", "ANIMATION = None\n")
        trust_mod.ensure(self.project, interactive=True, ask=lambda kinds, text: "deny")
        self.assertEqual(trust_mod.trusted_kinds(self.project), set())
        self.assertEqual(trust_mod.status(self.project, "animations"), "denied")
        called = []
        trust_mod.ensure(self.project, interactive=True, ask=lambda kinds, text: called.append(kinds) or "deny")
        self.assertEqual(called, [])

        _write(anim, "ANIMATION = 'changed'\n")
        self.assertIn("animations", trust_mod.pending_kinds(self.project))

    def test_trust_repo_flag_approves_without_asking(self) -> None:
        _write(self.project["animations"] / "glow.py", "ANIMATION = None\n")

        def ask(kinds, text):
            raise AssertionError("--trust-repo 不应询问")

        trusted = trust_mod.ensure(self.project, interactive=True, always=True, ask=ask)
        self.assertEqual(trusted, {"animations"})
        self.assertEqual(trust_mod.status(self.project, "animations"), "always")

    def test_non_interactive_with_trust_repo_loads(self) -> None:
        _write(self.project["memory_engines"] / "proj.py", "ENGINE = lambda data_dir: None\n")
        trusted = trust_mod.ensure(self.project, interactive=False, always=True)
        self.assertEqual(trusted, {"memory_engines"})

    def test_status_reports_empty_and_pending(self) -> None:
        self.assertEqual(trust_mod.status(self.project, "animations"), "empty")
        _write(self.project["animations"] / "glow.py", "ANIMATION = None\n")
        self.assertEqual(trust_mod.status(self.project, "animations"), "pending")


class EngineLoadTest(unittest.TestCase):
    def test_project_engine_loads_only_with_explicit_dir(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = root / "loaded.txt"
        _write(root / "proj_engine.py",
               "from pathlib import Path\n"
               f"Path({str(marker)!r}).write_text('x')\n"
               "ENGINE = lambda data_dir: object()\n")
        data = root / "data"
        with self.assertRaises(Exception):
            registry_mod.get_engine("proj_engine", data)
        self.assertFalse(marker.exists())

        engine = registry_mod.get_engine("proj_engine", data, extra_dir=root)
        self.assertIsNotNone(engine)
        self.assertTrue(marker.exists())


class AnimationGateTest(unittest.TestCase):
    def test_project_animation_only_loads_with_explicit_dir(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        anim_dir = root / "anims"
        marker = root / "ran.txt"
        _write(anim_dir / "evil.py",
               "from pathlib import Path\n"
               f"Path({str(marker)!r}).write_text('x')\n"
               "class A:\n"
               "    min_columns = 0\n"
               "    min_rows = 0\n"
               "    @staticmethod\n"
               "    def frames(ctx):\n"
               "        return iter(())\n"
               "ANIMATION = A\n")
        with self.assertRaises(animations.AnimationError):
            animations.load("evil")
        self.assertFalse(marker.exists())

        animation = animations.load("evil", dirs=(anim_dir,))
        self.assertTrue(marker.exists())
        self.assertTrue(callable(getattr(animation, "frames", None)))


class TrustCommandTest(TrustBase):
    def test_list_and_revoke(self) -> None:
        _write(self.project["animations"] / "x.py", "ANIMATION = None\n")
        trust_mod.approve(self.project, ["animations"])
        state = SimpleNamespace(settings=Settings({"project.enabled": True}))
        with contextlib.chdir(self.root):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertTrue(handle_command(state, "/trust"))
            output = buf.getvalue()
            self.assertIn("已信任仓库", output)
            self.assertIn(str(self.project["root"]), output)
            revoke = io.StringIO()
            with contextlib.redirect_stdout(revoke):
                self.assertTrue(handle_command(state, "/trust revoke 1"))
            self.assertIn("已撤销", revoke.getvalue())
        self.assertEqual(trust_mod.entries(), {})

    def test_disabled_project_scope(self) -> None:
        state = SimpleNamespace(settings=Settings({"project.enabled": False}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/trust"))
        self.assertIn("项目级发现已关闭", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
