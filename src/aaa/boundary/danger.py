"""危险命令识别：只回答「这条命令要不要人工看一眼」。

不是沙箱，也不是路径约束：绝对路径、软链接、脚本内部的间接调用都能绕开它。
它的用途只有一个——把审批从「每条都问」降到「危险才问」时，仍然对不可逆
操作保留确认。规则按「类别 → 正则 → 给用户看的理由」声明，命中即停。

识别对象是命令类工具：
- `run_shell_command` 的参数是 argv 列表，按 POSIX 引用拼成一行再匹配；
- `run_powershell` 的参数是单条命令字符串。
两者共用同一套规则（PowerShell 有自己的破坏性动词规则）。
"""

import re
import shlex
from typing import Mapping, Optional, Pattern, Tuple

ShellArgs = Mapping[str, object]

DANGEROUS_RULES: Tuple[Tuple[str, Pattern[str], str], ...] = (
    ("rm-recursive", re.compile(r"\brm\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*[rR]", re.I), "递归删除"),
    ("rm-forced-root",
     re.compile(r"\brm\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*f[A-Za-z]*\s+(?:/|~|\$HOME|\*)", re.I),
     "强制删除根目录、家目录或通配"),
    ("mkfs", re.compile(r"\bmkfs(?:\.\w+)?\b", re.I), "格式化文件系统"),
    ("dd-device", re.compile(r"\bdd\b[^|;&\n]*\bof=/dev/", re.I), "直接写块设备"),
    ("power", re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.I), "关机或重启"),
    ("privilege", re.compile(r"\b(?:sudo|doas|pkexec)\b", re.I), "提权执行"),
    ("recursive-perm", re.compile(r"\b(?:chmod|chown)\s+-R\b", re.I), "递归改权限或属主"),
    ("download-exec",
     re.compile(r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?"
                r"(?:sh|bash|zsh|fish|python3?|node|perl|ruby)\b", re.I),
     "下载内容直接交给解释器执行"),
    ("git-destructive",
     re.compile(r"\bgit\s+(?:push\b[^\n]*\s(?:--force|-f\b)|reset\s+--hard\b"
                r"|clean\s+-[A-Za-z]*[fdx]|branch\s+-D\b)", re.I),
     "覆盖远端历史或丢弃本地改动"),
    ("kill-force",
     re.compile(r"\b(?:kill|pkill|killall)\b[^\n]*\s(?:-9|-KILL|-s\s+KILL)\b", re.I),
     "强制终止进程"),
    ("disk-ops",
     re.compile(r"\b(?:diskutil|hdiutil)\b[^\n]*\b(?:erase|delete|unmount)\b", re.I),
     "擦除或卸载磁盘"),
    ("credentials",
     re.compile(r"(?:^|[\s/'\"])(?:\.ssh/|\.aws/|\.gnupg/|\.aaa/secret\.yaml|\.env\b)", re.I),
     "读取密钥或凭证文件"),
    ("remote-transfer", re.compile(r"\bscp\b|\brsync\b[^\n]*\s\S+:", re.I), "向远端传输文件"),
    ("system-path", re.compile(r">\s*/(?:etc|usr|bin|sbin|System|Library|boot)/", re.I),
     "覆写系统路径"),
    ("system-service",
     re.compile(r"\b(?:launchctl\s+(?:unload|bootout|disable)"
                r"|systemctl\s+(?:stop|disable|mask))\b", re.I),
     "停用系统服务"),
    ("package-global",
     re.compile(r"\b(?:pip3?|npm|yarn|pnpm|brew|apt|apt-get|dnf|yum|pacman)\b\s+"
                r"(?:install|uninstall|remove|upgrade|add|i)\b", re.I),
     "改动全局或系统级软件包"),
    ("powershell-destructive",
     re.compile(r"\b(?:Remove-Item|Format-Volume|Clear-Disk|Set-ExecutionPolicy)\b", re.I),
     "PowerShell 破坏性操作"),
    ("powershell-elevate", re.compile(r"\bStart-Process\b[^\n]*-Verb\s+RunAs", re.I),
     "PowerShell 提权启动进程"),
)


def command_line(tool_name: str, args: Optional[ShellArgs]) -> str:
    """把命令类工具的参数还原成一行命令；不是命令类工具时返回空串。"""
    if not args:
        return ""
    if tool_name == "run_shell_command":
        argv = args.get("args")
        if isinstance(argv, str):
            return argv
        if isinstance(argv, (list, tuple)):
            return shlex.join(str(part) for part in argv)
        return ""
    if tool_name == "run_powershell":
        return str(args.get("command") or "")
    return ""


def danger_reason(tool_name: str, args: Optional[ShellArgs]) -> Optional[str]:
    """命中危险规则时返回给用户看的理由，否则返回 None。"""
    line = command_line(tool_name, args)
    if not line:
        return None
    for _, pattern, reason in DANGEROUS_RULES:
        if pattern.search(line):
            return reason
    return None
