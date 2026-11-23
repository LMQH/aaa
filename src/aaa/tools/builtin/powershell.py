"""PowerShell 工具：仅在 pwsh/powershell 可执行文件存在时装配。

Windows 上 shell 对应物是 PowerShell；本工具只包装它，不改写命令内容。
"""

import os
import shutil
import signal
import subprocess
from typing import List, Optional

from agno.tools import Toolkit

# 单次执行超时与输出上限，与 shell 工具保持一致。
TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 1_000_000
# 可执行文件名：新版跨平台 pwsh 优先，其次 Windows 自带的 powershell。
EXECUTABLE_NAMES = ("pwsh", "powershell", "powershell.exe")


def find_powershell(executable: Optional[str] = None) -> Optional[str]:
    """定位 PowerShell 可执行文件；显式给出时只检查存在性。"""
    if executable:
        return executable if os.path.exists(executable) else None
    for name in EXECUTABLE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


class PowerShellTools(Toolkit):
    """把 PowerShell 命令作为一个工具暴露；不解析、不改写命令。"""

    TIMEOUT_SECONDS = TIMEOUT_SECONDS
    MAX_OUTPUT_BYTES = MAX_OUTPUT_BYTES

    def __init__(
        self,
        executable: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        max_output_bytes: Optional[int] = None,
        default_tail: Optional[int] = None,
        **kwargs,
    ) -> None:
        self.timeout_seconds = int(timeout_seconds or TIMEOUT_SECONDS)
        self.max_output_bytes = int(max_output_bytes or MAX_OUTPUT_BYTES)
        self.default_tail = int(default_tail or 100)
        resolved = find_powershell(executable)
        if resolved is None:
            raise ValueError("未找到 pwsh 或 powershell，不能装配 PowerShell 工具")
        self.executable = resolved
        super().__init__(name="powershell_tools", tools=[self.run_powershell], **kwargs)

    def run_powershell(self, command: str, tail: Optional[int] = None) -> str:
        """Runs a PowerShell command and returns its output.

        Args:
            command: The PowerShell command line, executed with -NoProfile -NonInteractive.
            tail: The number of lines to return from the output.
        """
        lines = int(tail) if tail else self.default_tail
        argv: List[str] = [
            self.executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name != "nt"),
            )
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
                return f"Error: timeout after {self.TIMEOUT_SECONDS}s"
        except Exception as exc:
            return f"Error: {exc}"

        if process.returncode == 0:
            output = stdout
        else:
            output = (stderr or stdout).strip() or f"Error: exit code {process.returncode}"
        if len(output) > self.max_output_bytes:
            output = output[: self.max_output_bytes] + "\n[output truncated]"
        return "\n".join(output.split("\n")[-lines:])
