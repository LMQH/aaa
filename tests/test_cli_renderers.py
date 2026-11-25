"""可插拔外部渲染器：探测、围栏识别、渲染与失败降级、Markdown 流接管。"""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from aaa.cli import images, renderers
from aaa.cli.render import MarkdownStream

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

FAKE_MMDC = """#!/bin/sh
# 模拟 mermaid-cli：解析 -i/-o，写出一张 PNG，并记录调用次数。
while [ $# -gt 0 ]; do
  case "$1" in
    -i) src="$2"; shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '\\211PNG\\r\\n\\032\\n\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000' > "$out"
echo run >> "$(dirname "$out")/calls"
"""

FAIL_MMDC = """#!/bin/sh
echo "boom: bad diagram" >&2
exit 1
"""

SLOW_MMDC = """#!/bin/sh
sleep 5
"""


def _install(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipIf(os.name == "nt", "Windows 上不执行 shell 假渲染器")
class RendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cache = self.tmp / "cache"
        # 只含假命令目录 + 系统基础目录：既不撞上机器上真实安装的 mmdc，又保留 dirname 等基础命令。
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin"}

    def test_available_only_when_command_installed(self) -> None:
        self.assertEqual(renderers.available(self.env), {})
        _install(self.bin, "mmdc", FAKE_MMDC)
        self.assertEqual(list(renderers.available(self.env)), ["mermaid"])

    def test_fence_language_detection(self) -> None:
        self.assertEqual(renderers.fence_language("```mermaid\ngraph TD\n```"), "mermaid")
        self.assertEqual(renderers.fence_language("```python\nprint(1)\n```"), "python")
        self.assertIsNone(renderers.fence_language("just text"))
        self.assertIsNone(renderers.fence_language("```\ncode\n```"))

    def test_render_writes_png_and_caches(self) -> None:
        _install(self.bin, "mmdc", FAKE_MMDC)
        spec = renderers.available(self.env)["mermaid"]
        first = renderers.render(spec, "graph TD; A-->B", timeout=10, cache_dir=self.cache, env=self.env)
        self.assertIsNotNone(first)
        self.assertTrue(first.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        again = renderers.render(spec, "graph TD; A-->B", timeout=10, cache_dir=self.cache, env=self.env)
        self.assertEqual(first, again)
        calls = list(self.cache.rglob("calls"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].read_text().strip(), "run")

    def test_render_failure_returns_none(self) -> None:
        _install(self.bin, "mmdc", FAIL_MMDC)
        spec = renderers.available(self.env)["mermaid"]
        self.assertIsNone(renderers.render(spec, "broken", timeout=10, cache_dir=self.cache, env=self.env))

    def test_render_timeout_returns_none(self) -> None:
        _install(self.bin, "mmdc", SLOW_MMDC)
        spec = renderers.available(self.env)["mermaid"]
        started = time.time()
        self.assertIsNone(renderers.render(spec, "slow", timeout=0.5, cache_dir=self.cache, env=self.env))
        self.assertLess(time.time() - started, 4)


class MarkdownStreamHookTest(unittest.TestCase):
    def test_hook_takes_over_matching_fence(self) -> None:
        stream = MarkdownStream(width=80, color=False)
        seen = []
        stream.on_code_block = lambda language, code: seen.append((language, code)) or True
        out = stream.feed("```mermaid\ngraph TD; A-->B\n```\n")
        self.assertEqual(seen, [("mermaid", "graph TD; A-->B")])
        self.assertEqual(out, "")

    def test_hook_decline_falls_back_to_markdown(self) -> None:
        stream = MarkdownStream(width=80, color=False)
        stream.on_code_block = lambda language, code: False
        out = stream.feed("```mermaid\ngraph TD; A-->B\n```\n")
        self.assertIn("A-->B", out)

    def test_plain_blocks_never_reach_hook(self) -> None:
        stream = MarkdownStream(width=80, color=False)
        seen = []
        stream.on_code_block = lambda language, code: seen.append(language) or True
        stream.feed("# 标题\n\n普通段落\n")
        self.assertEqual(seen, [])

    def test_display_math_block_reaches_hook_with_inner_source(self) -> None:
        stream = MarkdownStream(width=80, color=False)
        seen = []
        stream.on_code_block = lambda language, code: seen.append((language, code)) or True
        stream.feed("公式：\n\n$$\nE = mc^2\n$$\n\n完毕。\n")
        self.assertEqual(seen, [("math", "E = mc^2")])


FAKE_LATEX = """#!/bin/sh
# 模拟 latex：OUTDIR <dir> <name> 写出同名 .dvi（连字符放变量里，便于内嵌）。
DASH=-
dir="."
while [ $# -gt 0 ]; do
  case "$1" in
    "${DASH}output-directory") dir="$2"; shift 2 ;;
    "${DASH}"*) shift ;;
    *) name="$1"; shift ;;
  esac
done
printf 'DVI' > "$dir/${name%.tex}.dvi"
echo latex >> "$dir/calls"
"""

FAKE_DVIPNG = """#!/bin/sh
# 模拟 dvipng：按 OUT 写出 PNG，要求 .dvi 参数指向存在的文件。
DASH=-
out=""
dvi=""
while [ $# -gt 0 ]; do
  case "$1" in
    "${DASH}o") out="$2"; shift 2 ;;
    "${DASH}T"|"${DASH}D") shift 2 ;;
    "${DASH}"*) shift ;;
    *) dvi="$1"; shift ;;
  esac
done
[ -f "$dvi" ] || { echo "no dvi" >&2; exit 1; }
printf '\\211PNG\\r\\n\\032\\n\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000' > "$out"
echo dvipng >> "$(dirname "$out")/calls"
"""


@unittest.skipIf(os.name == "nt", "Windows 上不执行 shell 假渲染器")
class MathPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cache = self.tmp / "cache"
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin"}

    def test_math_needs_every_step_installed(self) -> None:
        _install(self.bin, "latex", FAKE_LATEX)
        self.assertNotIn("math", renderers.available(self.env))
        _install(self.bin, "dvipng", FAKE_DVIPNG)
        self.assertEqual(list(renderers.available(self.env)), ["math"])

    def test_math_runs_both_steps_and_wraps_formula(self) -> None:
        _install(self.bin, "latex", FAKE_LATEX)
        _install(self.bin, "dvipng", FAKE_DVIPNG)
        spec = renderers.available(self.env)["math"]
        out = renderers.render(spec, r"\frac{a}{b}", timeout=10, cache_dir=self.cache, env=self.env)
        self.assertIsNotNone(out)
        self.assertTrue(out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((out.parent / "calls").read_text().split(), ["latex", "dvipng"])
        tex = (out.parent / "input.tex").read_text(encoding="utf-8")
        self.assertIn(r"\frac{a}{b}", tex)
        self.assertIn("\\documentclass", tex)
        # standalone 把正文装进盒子：行间公式标记在其中非法（真实 TeX 报 Missing $ inserted），
        # 必须用 $\displaystyle …$。假 latex 测不出这一点，这里锁定包装形态。
        self.assertIn("$\\displaystyle \\frac{a}{b}$", tex)
        self.assertNotIn("\\[", tex)

    def test_math_wrapper_strips_redundant_display_markers(self) -> None:
        wrap = renderers.RENDERERS["math"].wrap

        def body(formula: str) -> str:
            return wrap(formula).splitlines()[3]

        self.assertEqual(body("\\[\nx^2\n\\]"), "$\\displaystyle x^2$")
        self.assertEqual(body("$$x + y$$"), "$\\displaystyle x + y$")
        self.assertEqual(body("  \\int_0^1 x \\, dx  "), "$\\displaystyle \\int_0^1 x \\, dx$")

    def test_first_step_failure_stops_pipeline(self) -> None:
        _install(self.bin, "latex", FAIL_MMDC)
        _install(self.bin, "dvipng", FAKE_DVIPNG)
        spec = renderers.available(self.env)["math"]
        self.assertIsNone(renderers.render(spec, "x", timeout=10, cache_dir=self.cache, env=self.env))
        self.assertFalse(list(self.cache.rglob("calls")))


class SplitBlockTest(unittest.TestCase):
    def test_fenced_block(self) -> None:
        self.assertEqual(renderers.split_block("```mermaid\ngraph TD\n```"), ("mermaid", "graph TD"))

    def test_display_math_blocks(self) -> None:
        self.assertEqual(renderers.split_block("$$\nE = mc^2\n$$"), ("math", "E = mc^2"))
        self.assertEqual(renderers.split_block("$$E = mc^2$$"), ("math", "E = mc^2"))

    def test_prose_is_not_math(self) -> None:
        self.assertIsNone(renderers.split_block("$$x$$ 和 $$y$$ 都行"))
        self.assertIsNone(renderers.split_block("价格是 $$5"))
        self.assertIsNone(renderers.split_block("普通段落"))


class RealTexTest(unittest.TestCase):
    """装了真实 latex 与 dvipng 时才跑：用真 TeX 验证包装写法确实能出图。

    假命令测不出文档包装是否合法（standalone 里行间公式标记会报 Missing $ inserted），
    所以在有 TeX 的机器上保留这条真验证；未安装则跳过。
    """

    def test_real_tex_renders_formula_to_png(self) -> None:
        import shutil
        import tempfile

        if not (shutil.which("latex") and shutil.which("dvipng")):
            self.skipTest("未安装 latex/dvipng")
        spec = renderers.RENDERERS["math"]
        out = renderers.render(
            spec, r"\frac{a}{b} + \sqrt{x^2 + 1} = \sum_{i=1}^{n} x_i",
            timeout=120, cache_dir=Path(tempfile.mkdtemp(prefix="aaa-realtex-")),
        )
        self.assertIsNotNone(out, "真实 TeX 未产出图片：包装写法可能已退化")
        width, height = images.dimensions(out.read_bytes())
        self.assertGreater(width, 20)
        self.assertGreater(height, 10)


if __name__ == "__main__":
    unittest.main()
