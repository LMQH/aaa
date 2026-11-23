"""只发现外置引擎；发现不执行代码，启动只加载选中的插件。"""

import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from ..config.paths import MEMORY_ENGINES_DIR
from .protocol import MemoryEngineError

ENTRY_GROUP = "aaa.memory_engines"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _entry_points() -> Tuple:
    try:
        return tuple(importlib.metadata.entry_points(group=ENTRY_GROUP))
    except Exception:
        return ()


def _entry_map() -> Dict[str, object]:
    return {point.name: point for point in _entry_points()}


def engine_ids(extra_dir: Optional[Path] = None) -> Tuple[str, ...]:
    """可用外置标识；项目目录优先、本地文件其次、入口点兜底；不加载插件代码。"""
    dirs = [d for d in (extra_dir, MEMORY_ENGINES_DIR) if d is not None]
    local = sorted({path.stem for d in dirs for path in d.glob("*.py") if path.is_file()})
    return tuple(dict.fromkeys(
        name for name in (*local, *_entry_map()) if _valid_id(name)
    ))


def _valid_id(name: str) -> bool:
    return NAME.fullmatch(name) is not None


def _load_file(engine_id: str, path: Path) -> Callable:
    module_name = f"aaa_memory_engine_{engine_id}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MemoryEngineError(f"记忆引擎 {engine_id} 无法加载：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit) as exc:
        sys.modules.pop(module_name, None)
        raise MemoryEngineError(f"外置记忆引擎 {engine_id} 加载失败（{type(exc).__name__}）") from exc
    factory = getattr(module, "ENGINE", None)
    if not callable(factory):
        raise MemoryEngineError(f"记忆引擎 {engine_id} 缺少可调用的 ENGINE")
    return factory


def _factory(engine_id: str, extra_dir: Optional[Path] = None) -> Optional[Callable]:
    for directory in (extra_dir, MEMORY_ENGINES_DIR):
        if directory is None:
            continue
        candidate = directory / f"{engine_id}.py"
        if candidate.is_file():
            return _load_file(engine_id, candidate)
    point = _entry_map().get(engine_id)
    if point is None:
        return None
    try:
        factory = point.load()
    except (Exception, SystemExit) as exc:
        raise MemoryEngineError(f"外置记忆引擎 {engine_id} 加载失败（{type(exc).__name__}）") from exc
    if not callable(factory):
        raise MemoryEngineError(f"记忆引擎 {engine_id} 的入口不可调用")
    return factory


def get_engine(engine_id: str, data_root: Path, extra_dir: Optional[Path] = None):
    """用独立数据目录构造选中的外置引擎；项目目录优先于用户目录。"""
    if not _valid_id(engine_id):
        raise MemoryEngineError(f"无法识别外置记忆引擎：{engine_id or '未选择'}")
    factory = _factory(engine_id, extra_dir)
    if factory is None:
        raise MemoryEngineError(f"找不到外置记忆引擎：{engine_id}")
    data_dir = data_root / engine_id
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        engine = factory(data_dir)
    except (Exception, SystemExit) as exc:
        raise MemoryEngineError(f"外置记忆引擎 {engine_id} 初始化失败（{type(exc).__name__}）") from exc
    return engine
