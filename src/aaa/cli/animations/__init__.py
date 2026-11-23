"""启动动画插件：协议、发现与加载。

一个动画就是一个 `.py` 文件，模块级导出 `ANIMATION`：

- 函数：`frames(ctx)` 的简写；
- 对象：需有 `frames(ctx)`，可选 `min_columns`、`min_rows`、`static(ctx)`。

`frames(ctx)` 每项产出 `Frame(delay, content)`；`content` 是字符串或
`columns -> 字符串` 的函数，绘制推迟到真正需要那一帧时。文件名即动画名，
`~/.aaa/animations/<name>.py` 覆盖包内同名文件。
"""

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Protocol, Tuple, Union

from ...config.paths import ANIMATIONS_DIR

DEFAULT = "default"
PACKAGE_DIR = Path(__file__).parent
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

Renderable = Union[str, Callable[[int], str]]


@dataclass(frozen=True)
class FrameContext:
    """动画可用的终端信息；`rows` 取播放开始时的行数。"""

    columns: int
    rows: int
    duration: float
    version: str


@dataclass(frozen=True)
class Frame:
    """一帧：停留秒数 + 内容。"""

    delay: float
    content: Renderable


class AnimationError(Exception):
    """动画文件缺失、无法导入或不符合契约。"""


class Animation(Protocol):
    def frames(self, ctx: FrameContext) -> Iterator[Frame]: ...


class _Function:
    """把裸函数包装成满足契约的对象，尺寸按 0 处理。"""

    min_columns = 0
    min_rows = 0

    def __init__(self, frames: Callable[[FrameContext], Iterator[Frame]]) -> None:
        self.frames = frames


def version() -> str:
    try:
        from importlib.metadata import version as dist_version

        return dist_version("aaa")
    except Exception:
        return "0.0.0"


def paint(content: Renderable, columns: int) -> str:
    return content if isinstance(content, str) else content(columns)


def discover(dirs=None) -> Dict[str, Path]:
    """动画名 → 文件；`dirs` 按优先级在前，包内目录兜底。"""
    found: Dict[str, Path] = {}
    order = (*dirs, PACKAGE_DIR) if dirs is not None else (ANIMATIONS_DIR, PACKAGE_DIR)
    for directory in order:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_") or not _NAME.fullmatch(path.stem):
                continue
            found.setdefault(path.stem, path)
    return found


def normalize(target: Any, path: Path) -> Any:
    if target is None:
        raise AnimationError(f"{path}: 缺少模块级 ANIMATION")
    if callable(target) and not hasattr(target, "frames"):
        target = _Function(target)
    if not callable(getattr(target, "frames", None)):
        raise AnimationError(f"{path}: ANIMATION 需要 frames(ctx)")
    return target


def load(name: str, path: Optional[Path] = None, dirs=None) -> Any:
    """按名或显式文件加载动画；任何失败都抛 `AnimationError`。"""
    if path is None:
        path = discover(dirs).get(name)
        if path is None:
            raise AnimationError(f"未找到动画 {name!r}")
    module_name = f"{__name__}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AnimationError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise AnimationError(f"{path}: {exc}") from exc
    return normalize(getattr(module, "ANIMATION", None), path)


def limits(animation: Any) -> Tuple[int, int]:
    """动画自行声明的最小列数/行数；未声明视为 0。"""
    return int(getattr(animation, "min_columns", 0)), int(getattr(animation, "min_rows", 0))


def frames_of(animation: Any, ctx: FrameContext) -> Iterator[Frame]:
    for frame in animation.frames(ctx):
        if not isinstance(frame, Frame):
            raise AnimationError("frames(ctx) 必须产出 Frame(delay, content)")
        yield frame


def static_of(animation: Any, ctx: FrameContext) -> Renderable:
    """静置画面：优先用动画的 `static(ctx)`，否则取第一帧。"""
    static = getattr(animation, "static", None)
    if callable(static):
        return static(ctx)
    for frame in frames_of(animation, ctx):
        return frame.content
    raise AnimationError("动画没有产出任何帧")
