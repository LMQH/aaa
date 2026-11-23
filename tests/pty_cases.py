"""PTY 驱动的交互面用例：HITL 审批与 /provide 向导。

独立文件，避免把 PTY 的噪声塞进 e2e.py；由 `e2e.py --pty` 导入调用。
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PYTHON = os.environ.get("AAA_PYTHON", sys.executable)

# 向导交互的等待上限：探查模型需要一次网络请求。
PROMPT_TIMEOUT = 120.0


class PtyRepl:
    """把 REPL 放进伪终端，按文本等待与发送按键。"""

    def __init__(self, home: Path, work: Path) -> None:
        env = os.environ.copy()
        env.pop("ALL_PROXY", None)
        env.pop("all_proxy", None)
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(SRC)
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "aaa"],
            cwd=str(work),
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        self.buffer = ""

    def send(self, text: str) -> None:
        os.write(self.master, text.encode())

    def wait_for(self, pattern: str, timeout: float = PROMPT_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = os.read(self.master, 4096).decode("utf-8", "replace")
            except OSError:
                return False
            if not chunk:
                break
            self.buffer += chunk
            if pattern in self.buffer:
                return True
        return False

    def close(self) -> str:
        try:
            self.send("/exit\r")
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.close(self.master)
        except OSError:
            pass
        return self.buffer


def _home(with_config: bool) -> Path:
    home = Path(tempfile.mkdtemp(prefix="aaa-pty-"))
    (home / ".aaa").mkdir(parents=True, exist_ok=True)
    if with_config:
        real = Path.home() / ".aaa" / "secret.yaml"
        (home / ".aaa" / "secret.yaml").write_bytes(real.read_bytes())
        os.chmod(home / ".aaa" / "secret.yaml", 0o600)
    return home


def hitl_case(record) -> None:
    """P2.11：workspace-write 下真实审批放行一次 shell。"""
    name = "P2.11 HITL 真实确认（PTY）"
    repl = PtyRepl(_home(True), Path(tempfile.mkdtemp(prefix="aaa-ws-")))
    try:
        if not repl.wait_for("命令"):
            record(name, "FAIL", "REPL 未启动")
            return
        repl.send("/mode workspace-write\r")
        time.sleep(1)
        repl.send("运行 echo hitl-ok 并告诉我输出\r")
        if not repl.wait_for("允许执行"):
            record(name, "FAIL", f"未出现审批提示；尾部：{repl.buffer[-200:]}")
            return
        repl.send("y\r")
        if repl.wait_for("hitl-ok", timeout=180):
            record(name, "PASS", "审批后执行成功")
        else:
            record(name, "FAIL", f"审批后未见输出；尾部：{repl.buffer[-200:]}")
    finally:
        repl.close()


def wizard_case(record, api_key: str) -> None:
    """P2.12：首次启动的 /provide 向导走真实探查并落盘。"""
    name = "P2.12 /provide 真实向导（PTY）"
    home = _home(False)
    secret = home / ".aaa" / "secret.yaml"
    repl = PtyRepl(home, Path(tempfile.mkdtemp(prefix="aaa-ws-")))
    try:
        if not repl.wait_for("首次启动"):
            record(name, "FAIL", f"未进入向导；尾部：{repl.buffer[-200:]}")
            return
        if not repl.wait_for("渠道 id"):
            record(name, "FAIL", f"未询问渠道 id；尾部：{repl.buffer[-200:]}")
            return
        repl.send("dsofficial\r")
        if not repl.wait_for("协议"):
            record(name, "FAIL", f"未询问协议；尾部：{repl.buffer[-200:]}")
            return
        repl.send("\r")  # 默认 deepseek 原生
        if not repl.wait_for("Base URL"):
            record(name, "FAIL", f"未询问 Base URL；尾部：{repl.buffer[-200:]}")
            return
        repl.send("\r")  # 默认 https://api.deepseek.com
        if not repl.wait_for("API Key"):
            record(name, "FAIL", f"未询问 API Key；尾部：{repl.buffer[-200:]}")
            return
        repl.send(api_key + "\r")
        if not repl.wait_for("启用哪些模型", timeout=PROMPT_TIMEOUT):
            record(name, "FAIL", f"探查后未出现模型清单；尾部：{repl.buffer[-400:]}")
            return
        repl.send(" ")  # 勾选第一项
        time.sleep(0.3)
        repl.send("\r")
        if not repl.wait_for("上下文窗口"):
            record(name, "FAIL", f"未询问上下文窗口；尾部：{repl.buffer[-300:]}")
            return
        repl.send("\r")  # 接受探查到的默认值
        if not repl.wait_for("默认模型"):
            record(name, "FAIL", f"未询问默认模型；尾部：{repl.buffer[-300:]}")
            return
        repl.send("\r")
        if not repl.wait_for("已保存"):
            record(name, "FAIL", f"未落盘；尾部：{repl.buffer[-300:]}")
            return

        import yaml

        data = yaml.safe_load(secret.read_text(encoding="utf-8"))
        channel = data["providers"]["dsofficial"]
        mode = secret.stat().st_mode & 0o777
        if mode != 0o600:
            record(name, "FAIL", f"落盘权限 {oct(mode)}，期望 0o600")
            return
        record(
            name,
            "PASS",
            f"渠道 {channel['api']} · {len(channel['models'])} 个模型 · 默认 {data['default']}",
        )
    finally:
        repl.close()
