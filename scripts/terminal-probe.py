#!/usr/bin/env python3
"""终端键位探针：开启 modifyOtherKeys 后逐键打印实际收到的字节。

用途：物理终端键位矩阵实测（iTerm2 / Terminal.app / tmux 各跑一次）。
在目标终端里执行 `python scripts/terminal-probe.py`，按提示依次按键，按 q 退出；
无论正常退出还是异常，都会关闭扩展键并恢复终端模式。

判读：
- Shift+Enter 显示 `'\x1b[27;2;13~'` → 该终端支持 modifyOtherKeys；
- 只显示 `'\r'`（或 `^M`）→ 不支持，Shift+Enter 与 Enter 无法区分；
- 其它序列原样打印，记录进 HANDOFF 的键位矩阵。
"""

import os
import sys
import termios
import tty

ENABLE = "\x1b[>4;2m"
DISABLE = "\x1b[>4m"

PROMPT = (
    "已发送 modifyOtherKeys 开启序列。依次按：Shift+Enter、Alt+Enter、Ctrl+Enter、Shift+Tab；"
    "按 q 退出。\n"
)


def describe(chunk: bytes) -> str:
    text = chunk.decode("utf-8", "replace")
    points = " ".join(f"U+{ord(ch):04X}" for ch in text)
    return f"{text!r}  [{points}]"


def main() -> int:
    stdin = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("需要在真实终端里运行：stdin 不是 tty。", file=sys.stderr)
        return 2
    old = termios.tcgetattr(stdin)
    try:
        # raw：不做 ICRNL/ISIG 转换，看到的就是终端实际发出的字节。
        tty.setraw(stdin)
        # raw 会连带关闭 OPOST（换行不再补回车），这里恢复，避免输出行行递进缩进。
        patched = termios.tcgetattr(stdin)
        patched[1] |= termios.OPOST
        termios.tcsetattr(stdin, termios.TCSADRAIN, patched)
        sys.stdout.write(ENABLE)
        sys.stdout.write(PROMPT)
        sys.stdout.flush()
        while True:
            data = os.read(stdin, 64)
            if not data:
                break
            sys.stdout.write(describe(data) + "\n")
            sys.stdout.flush()
            if data in (b"q", b"\x03"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(DISABLE)
        sys.stdout.flush()
        termios.tcsetattr(stdin, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
