"""Boundary：只声明「允许什么」与「隔离到什么范围」。

- 不装配工具对象（装配在 `tools.assemble`）。
- 不读取配置（配置在 `config`；这里只接受调用方给出的基准目录）。
- 隔离以值对象表达，不写死执行方式：当前由调用方把它落实到工具参数上，
  未来沙箱接入时消费同一份声明。

替换点：`resolve_policy` 是纯函数，签名即 `PolicySource`。调用方可以换成
自己的实现（例如沙箱插件返回自带隔离句柄的策略），不必改工具或运行时。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, FrozenSet, Optional

from .danger import danger_reason

MODE_READ = "read-only"
MODE_WRITE = "workspace-write"
MODE_FULL = "danger-full-access"
MODES = [MODE_READ, MODE_WRITE, MODE_FULL]

# 审批策略：作用于「已确定需要确认」的工具（`ModePolicy.confirm_tools`）。
#   always    —— 每次调用都问人（默认）
#   dangerous —— 只有命中 `danger` 规则的那次调用才问人
# `danger-full-access` 的 `confirm_tools` 为空，因此任何策略都不会触发询问。
APPROVAL_ALWAYS = "always"
APPROVAL_DANGEROUS = "dangerous"
APPROVAL_STRATEGIES = [APPROVAL_ALWAYS, APPROVAL_DANGEROUS]

# 能力名是两个面之间唯一的共享词汇，与 `tools.assemble` 的装配键一致。
CAP_READ, CAP_LIST, CAP_GREP = "read", "list", "grep"
CAP_WRITE, CAP_EDIT, CAP_MOVE, CAP_DELETE = "write", "edit", "move", "delete"
CAP_SHELL, CAP_POWERSHELL = "shell", "powershell"


@dataclass(frozen=True)
class Isolation:
    """隔离范围：文件工具能碰到的根、命令的工作目录。

    这里只描述「范围」，不描述「怎么拦」。当前调用方把 `file_root` 交给文件类
    工具、把 `command_cwd` 作为命令的工作目录；沙箱接入后由沙箱消费同一份声明。
    """

    file_root: Path
    command_cwd: Path


@dataclass(frozen=True)
class ModePolicy:
    """一种权限模式下的完整声明。"""

    capabilities: FrozenSet[str]
    confirm_tools: FrozenSet[str]
    isolation: Isolation


def resolve_policy(mode: str, base: Optional[Path] = None) -> ModePolicy:
    """默认实现：三种模式的声明。`base` 是调用方给出的基准目录，缺省为 cwd。"""
    base = Path(base) if base is not None else Path.cwd()
    if mode == MODE_READ:
        return ModePolicy(
            capabilities=frozenset({CAP_READ, CAP_LIST, CAP_GREP}),
            confirm_tools=frozenset(),
            isolation=Isolation(file_root=base, command_cwd=base),
        )
    if mode == MODE_WRITE:
        return ModePolicy(
            capabilities=frozenset(
                {CAP_READ, CAP_LIST, CAP_GREP, CAP_WRITE, CAP_EDIT, CAP_MOVE,
                 CAP_SHELL, CAP_POWERSHELL}
            ),
            confirm_tools=frozenset(
                {"run_shell_command", "run_powershell", "write_file", "edit_file", "move_file"}
            ),
            isolation=Isolation(file_root=base, command_cwd=base),
        )
    if mode == MODE_FULL:
        return ModePolicy(
            capabilities=frozenset(
                {CAP_READ, CAP_LIST, CAP_GREP, CAP_WRITE, CAP_EDIT, CAP_MOVE,
                 CAP_DELETE, CAP_SHELL, CAP_POWERSHELL}
            ),
            confirm_tools=frozenset(),
            isolation=Isolation(file_root=Path("/"), command_cwd=base),
        )
    raise KeyError(f"未知权限模式：{mode}")


# 调用方可以换成自己的实现来替换权限/隔离声明（如沙箱插件）。
PolicySource = Callable[[str, Optional[Path]], ModePolicy]

# 内容审阅类工具：批准针对改动内容（diff），且不受审批策略自动放行影响——
# `dangerous` 策略只对命令类工具生效，文件改动每次都要过目。
REVIEW_FILE_TOOLS = frozenset({"write_file", "edit_file", "move_file"})

# 外部信任面的工具名前缀：第三方代码（MCP 插件）与编程式扩展注册的工具，
# 每次询问、审批策略不自动放行。
EXTERNAL_TOOL_PREFIXES = ("mcp_", "ext_")

# 自家但改动用户配置的工具：同样每次询问、不自动放行。
ALWAYS_ASK_TOOLS = frozenset({"create_plugin", "create_skill"})


def is_external_tool(name: str) -> bool:
    return name.startswith(EXTERNAL_TOOL_PREFIXES) or name in ALWAYS_ASK_TOOLS


def capabilities(mode: str) -> FrozenSet[str]:
    """该模式允许的能力名集合。"""
    return resolve_policy(mode).capabilities


def confirm_tools(mode: str) -> FrozenSet[str]:
    """该模式下需要人工确认的工具名。"""
    return resolve_policy(mode).confirm_tools


def root(mode: str) -> Path:
    """文件类工具的根：danger-full-access 为 /，其余为 cwd。"""
    return resolve_policy(mode).isolation.file_root


def isolation(mode: str, base: Optional[Path] = None) -> Isolation:
    """该模式的隔离范围声明。"""
    return resolve_policy(mode, base).isolation


def signature(tool_name: str, args) -> str:
    """审批签名的键：工具名 + 参数（键序无关）。

    「什么算同一次批准」是策略，归本层；「怎么问、记不记得」是界面，归 `cli`。
    """
    if not args:
        return tool_name
    return f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"


def needs_approval(tool_name: str, args, strategy: str) -> bool:
    """已确定该工具需要确认后，判断这一次调用是否真的要问人。

    调用方只在工具名属于 `ModePolicy.confirm_tools` 时才该走到这里；
    `danger-full-access` 的确认名单为空，任何策略都不会询问。
    """
    if strategy == APPROVAL_ALWAYS:
        return True
    if strategy == APPROVAL_DANGEROUS:
        return danger_reason(tool_name, args) is not None
    raise KeyError(f"未知审批策略：{strategy}")

