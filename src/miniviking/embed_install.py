"""可选增强的安装器：把 embedding extra 的依赖装进 aaa 正在使用的环境。

策略按环境探测依次尝试（uv → pip → pipx）；默认源失败后自动改用国内镜像
重试一次——镜像只作为本次命令参数，不修改用户全局配置。全程失败时给出
可复制的手动命令。安装完成后需重启 aaa 生效（模型权重在下次启动自动下载）。

CLI 通过 `install` 在后台线程调用，`log` 接收进度行；本模块不做任何打印。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys

logger = logging.getLogger("aaa.memory.miniviking")

PACKAGES = ("llama-cpp-python>=0.3.0", "huggingface_hub>=0.34", "sqlite-vec>=0.1.9")
FALLBACK_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
_TIMEOUT = 1800
_EXTRA_MODULES = ("llama_cpp", "huggingface_hub", "sqlite_vec")


def extra_installed() -> bool:
    """嵌入增强是否已可导入。"""
    return all(
        importlib.util.find_spec(name) is not None for name in _EXTRA_MODULES
    )


def install(log, *, runner=None) -> bool:
    """按策略安装增强；`log` 接收进度行，返回是否成功。

    `runner` 仅供测试注入；默认 `subprocess.run`（捕获输出、限时 30 分钟）。
    """
    run = runner or _run
    strategies = _strategies()
    if not strategies:
        log("未找到可用的安装工具（uv / pip / pipx）。请手动执行：")
        log(f"  {sys.executable} -m pip install -i {FALLBACK_INDEX} " + " ".join(PACKAGES))
        return False
    rounds = [("默认源", None), ("国内镜像", FALLBACK_INDEX)]
    tried: set[str] = set()
    for round_label, index_url in rounds:
        for name, base in strategies:
            if (name, round_label) in tried:
                continue
            tried.add((name, round_label))
            argv = base if index_url is None else _with_index(name, base, index_url)
            source = "用户默认源" if index_url is None else "国内镜像"
            log(f"通过 {name} 安装（{source}）……")
            result = run(argv)
            if result.returncode == 0:
                log("安装成功。重启 aaa 后生效，模型权重将在下次启动自动下载。")
                return True
            log(f"{name} 安装失败（{source}）。")
            output = (result.stderr or "") + (result.stdout or "")
            for line in output.strip().splitlines()[-6:]:
                log(f"  {line}")
    log("自动安装失败。请手动执行：")
    log(f"  {sys.executable} -m pip install -i {FALLBACK_INDEX} " + " ".join(PACKAGES))
    return False


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT)


def _strategies() -> list[tuple[str, list[str]]]:
    strategies: list[tuple[str, list[str]]] = []
    uv = shutil.which("uv")
    if uv:
        strategies.append(("uv", [uv, "pip", "install", "--python", sys.executable, *PACKAGES]))
    if _has_pip():
        strategies.append(("pip", [sys.executable, "-m", "pip", "install", *PACKAGES]))
    pipx = shutil.which("pipx")
    if pipx:
        strategies.append(("pipx", [pipx, "inject", "aaa", *PACKAGES]))
    return strategies


def _has_pip() -> bool:
    try:
        check = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0


def _with_index(name: str, base: list[str], index_url: str) -> list[str]:
    if name == "pipx":
        return base + ["--pip-args", f"-i {index_url}"]
    return base + ["-i", index_url]
