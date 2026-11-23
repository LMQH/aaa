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

        def ask(kind, files):
            raise AssertionError("非交互模式不应询问")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            trusted = trust_mod.ensure(self.project, interactive=False, ask=ask)
        self.assertEqual(trusted, set())
        self.assertIn("非交互", err.getvalue())
        self.assertEqual(trust_mod.entries(), {})

    def test_interactive_approves_and_records(self) -> None:
        _write(self.project["extensions"] / "toy" / "extension.toml", '[mcp]\ncommand = "x"\n')
        seen = []

        def ask(kind, files):
            seen.append((kind, list(files)))
            return True

        trusted = trust_mod.ensure(self.project, interactive=True, ask=ask)
        self.assertIn("extensions", trusted)
        self.assertEqual(seen[0][0], "extensions")
        self.assertTrue(seen[0][1])
        self.assertIn("extensions", trust_mod.trusted_kinds(self.project))

    def test_interactive_deny_keeps_untrusted(self) -> None:
        _write(self.project["animations"] / "evil.py", "ANIMATION = None\n")
        trusted = trust_mod.ensure(self.project, interactive=True, ask=lambda kind, files: False)
        self.assertEqual(trusted, set())
        self.assertEqual(trust_mod.entries(), {})


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
