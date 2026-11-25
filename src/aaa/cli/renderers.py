"""可插拔外部渲染器：把特定内容的代码块交给外部命令渲染成图片。

约定：一个渲染器 = 一种内容（围栏语言或 `$$` 公式块）+ 一条外部命令流水线；
流水线接收源码文件，末步写出 PNG。命令未安装时该内容不注册，照常显示为源码——
对未安装者零影响。渲染在工作线程里做，调用方拿到 PNG 路径后走终端图片输出。
"""

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderStep:
    """流水线的一步：一个外部命令及其参数。"""

    command: str
    # (工作目录, 源码文件, 输出 PNG) → 命令参数
    args: Callable[[Path, Path, Path], List[str]]


@dataclass(frozen=True)
class RendererSpec:
    """一种内容对应的外部渲染流水线。"""

    language: str
    suffix: str                                   # 源码文件后缀，如 ".mmd"、".tex"
    steps: Tuple[RenderStep, ...]
    # 把块内源码整形成命令需要的输入；默认原样。
    wrap: Callable[[str], str] = lambda source: source


def _tex_source(formula: str) -> str:
    """把公式包成最小 LaTeX 文档（standalone 类负责裁边）。

    standalone 把正文装进盒子里，行间公式标记（\\[…\\]、$$…$$）在盒子里非法
    （真实 TeX 报 ``Missing $ inserted``），所以用 $\\displaystyle …$；
    公式若自带一层行间标记，先剥掉，避免嵌套。
    """
    body = formula.strip()
    for left, right in (("\\[", "\\]"), ("$$", "$$")):
        if body.startswith(left) and body.endswith(right) and len(body) > len(left) + len(right):
            body = body[len(left):-len(right)].strip()
    return (
        "\\documentclass[border=2pt]{standalone}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\begin{document}\n"
        f"$\\displaystyle {body}$\n"
        "\\end{document}\n"
    )


RENDERERS: Dict[str, RendererSpec] = {
    "mermaid": RendererSpec(
        language="mermaid", suffix=".mmd",
        steps=(RenderStep("mmdc", lambda work, src, out: ["-i", str(src), "-o", str(out), "-b", "transparent"]),),
    ),
    # 公式：经典 TeX 链。`latex` 产出同名 .dvi，`dvipng` 裁紧边后转 PNG。
    # 需要 MacTeX 或含 standalone 类的 TeX Live；缺任一命令时公式块照常显示源码。
    "math": RendererSpec(
        language="math", suffix=".tex",
        steps=(
            RenderStep(
                "latex",
                lambda work, src, out: ["-interaction=nonstopmode", "-halt-on-error",
                                        "-output-directory", str(work), src.name],
            ),
            RenderStep(
                "dvipng",
                lambda work, src, out: ["-D", "200", "-T", "tight", "-o", str(out), str(src.with_suffix(".dvi"))],
            ),
        ),
        wrap=_tex_source,
    ),
}


def available(env: Optional[dict] = None) -> Dict[str, RendererSpec]:
    """当前机器上流水线命令齐全的渲染器，按内容语言索引。"""
    env = os.environ if env is None else env
    return {
        language: spec for language, spec in RENDERERS.items()
        if all(shutil.which(step.command, path=env.get("PATH")) is not None for step in spec.steps)
    }


def fence_language(block: str) -> Optional[str]:
    """整块恰为一个代码围栏时返回其语言；否则 None。"""
    lines = block.strip("\n").split("\n")
    if len(lines) < 2 or not lines[0].startswith("```") or lines[-1].strip() != "```":
        return None
    return lines[0][3:].strip().lower() or None


def split_block(block: str) -> Optional[Tuple[str, str]]:
    """整块是可渲染内容时返回 (语言, 块内源码)。

    认两种形态：带语言的代码围栏（```mermaid … ```）与 `$$` 包裹的公式块
    （模型写公式更常用后者）。其余返回 None。
    """
    text = block.strip()
    language = fence_language(block)
    if language is not None:
        lines = text.split("\n")
        return language, "\n".join(lines[1:-1])
    if text.startswith("$$") and text.endswith("$$") and len(text) > 4:
        inner = text[2:-2].strip()
        if "$$" not in inner:
            return "math", inner
    return None


def render(
    spec: RendererSpec, source: str, *, timeout: float, cache_dir: Path, env: Optional[dict] = None,
) -> Optional[Path]:
    """按流水线把源码渲染成 PNG；任一步失败或超时、未产出文件都返回 None。

    产物按「流水线 + 源码」哈希缓存在 `cache_dir` 下，同一段源码只渲染一次。
    `env` 应与 `available()` 探测时使用的一致，避免探测到 A 命令却执行到 B。
    """
    pipeline = " ".join(step.command for step in spec.steps)
    key = hashlib.sha256(f"{pipeline}\0{source}".encode("utf-8")).hexdigest()[:32]
    work = cache_dir / f"{spec.language}-{key}"
    src, out = work / f"input{spec.suffix}", work / "output.png"
    if out.is_file() and out.stat().st_size > 0:
        return out
    try:
        work.mkdir(parents=True, exist_ok=True)
        src.write_text(spec.wrap(source), encoding="utf-8")
        for step in spec.steps:
            completed = subprocess.run(
                [step.command, *step.args(work, src, out)],
                capture_output=True, text=True, timeout=timeout, cwd=str(work), env=env,
            )
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip()[-300:]
                logger.warning("渲染器 %s 的 %s 步失败（退出码 %s）：%s",
                               spec.language, step.command, completed.returncode, tail)
                return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("渲染器 %s 执行失败：%s", spec.language, exc)
        return None
    if not out.is_file() or out.stat().st_size == 0:
        logger.warning("渲染器 %s 未产出图片", spec.language)
        return None
    return out
