"""启动参数：交互 REPL 与非交互执行共用一套解析。"""

import argparse
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import List, Optional

from ..boundary.policy import MODES

# 输出形态：interactive 进入 REPL，print 只打印正文，json 每行一个事件。
OUTPUT_MODES = ["interactive", "print", "json"]


def _package_version() -> str:
    """安装后取发行版本；源码运行回落开发版本。"""
    try:
        return version("amaterasu-agno-agent")
    except PackageNotFoundError:
        return "0.0.0+dev"


@dataclass(frozen=True)
class Options:
    message: Optional[str] = None
    output: str = "interactive"
    model: Optional[str] = None
    permission: Optional[str] = None
    continue_session: bool = False
    resume: Optional[str] = None
    # 非交互模式下放行需要确认的工具（当前只有 shell）；默认拒绝。
    allow_shell: bool = False
    # 直接信任当前仓库的项目级可执行扩展（始终），不再询问。
    trust_repo: bool = False
    # 随消息发送的本地图片路径（可重复）。
    images: List[str] = field(default_factory=list)

    @property
    def interactive(self) -> bool:
        return self.output == "interactive"


def build_parser(default_mode: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aaa",
        description="终端 agent harness。不带参数进入交互 REPL。",
    )
    parser.add_argument("--version", action="version", version=f"aaa {_package_version()}")
    parser.add_argument("message", nargs="?", help="首条消息；非交互模式下省略则从 stdin 读取")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-p", "--print", dest="output", action="store_const", const="print",
        help="单轮执行，只输出正文后退出",
    )
    group.add_argument(
        "--mode", dest="output", choices=OUTPUT_MODES,
        help="输出形态：interactive / print / json",
    )
    parser.add_argument("-m", "--model", metavar="REF", help="本轮使用的模型，形如 <渠道>/<模型>")
    parser.add_argument(
        "--permission", choices=MODES, default=default_mode,
        help=f"权限模式，默认 {default_mode}",
    )
    session = parser.add_mutually_exclusive_group()
    session.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                         help="续接当前目录最近的 Agent 会话")
    session.add_argument("-r", "--resume", metavar="ID", help="按会话 id 或前缀续接 Agent 会话")
    parser.add_argument(
        "--image", dest="images", action="append", default=[], metavar="PATH",
        help="随消息发送本地图片（可重复）；仅支持模型多模态时有效",
    )
    parser.add_argument(
        "--allow-shell", action="store_true",
        help="非交互模式下自动放行 shell 命令（默认拒绝）；权限模式仍需至少 workspace-write",
    )
    parser.add_argument(
        "--trust-repo", action="store_true",
        help="信任当前仓库的项目级可执行扩展（始终），跳过交互确认",
    )
    return parser


def parse_args(default_mode: str, argv: Optional[List[str]] = None) -> Options:
    namespace = build_parser(default_mode).parse_args(argv)
    return Options(
        message=namespace.message,
        output=namespace.output or "interactive",
        model=namespace.model,
        permission=namespace.permission,
        continue_session=namespace.continue_session,
        resume=namespace.resume,
        allow_shell=namespace.allow_shell,
        trust_repo=namespace.trust_repo,
        images=list(namespace.images),
    )
