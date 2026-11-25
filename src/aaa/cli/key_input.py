"""Normalize modified-key sequences before prompt_toolkit's VT100 parser.

终端只有在应用主动开启 xterm ``modifyOtherKeys`` 后才会把 Shift+Enter 与 Enter
区分开来；开启后所有带修饰键的按键都会被重新编码成 ``ESC[27;<mod>;<cp>~``
（Kitty CSI-u 为 ``ESC[<cp>;<mod>u``），而 prompt_toolkit 几乎不认这些序列。
这里在字节层把它们还原成传统字节/键串，既有键位无需改动即可继续生效。
"""

import os
import subprocess
import sys
from contextlib import contextmanager
from typing import Optional

from prompt_toolkit.input.defaults import create_input

if os.name != "nt":
    from prompt_toolkit.input.vt100 import Vt100Input
else:
    # prompt_toolkit 的 VT100 输入模块在 Windows 上导入即断言失败；那边走 Win32 输入，
    # 这里只在 POSIX 分支定义改造过的输入类，Windows 上保持 None。
    Vt100Input = None

# Shift+Enter 的等价形式；marker 是 prompt_toolkit 唯一能携带原始序列的形态。
MARKER = "\x1b[27;2;13~"
SHIFT_ENTER_SEQUENCES = (
    MARKER,
    "\x1b[27;6;13~",
    "\x1b[13;2u",
    "\x1b[13;2:1u",
    "\x1b[13;2:2u",
)
# 终端把 Shift+Enter 映射成 Esc+Enter 时也按换行处理；Esc 单独按下仍是取消。
ALT_ENTER_SEQUENCES = ("\x1b\r", "\x1b\n")
_SHIFT_RELEASE = "\x1b[13;2:3u"
_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_ENTER_TOKENS = (*SHIFT_ENTER_SEQUENCES, *ALT_ENTER_SEQUENCES, _SHIFT_RELEASE)
_TOKENS = (*_ENTER_TOKENS, _PASTE_START, _PASTE_END)
_CSI = "\x1b["
_PARAM_CHARS = frozenset("0123456789;:")
_MAX_SEQUENCE = 64
# Ctrl+2..Ctrl+8 在 xterm 键表里产出固定 C0 字节，没有可推导的位运算。
_CONTROL_DIGITS = {50: "\x00", 51: "\x1b", 52: "\x1c", 53: "\x1d", 54: "\x1e", 55: "\x1f", 56: "\x7f"}

# xterm modifyOtherKeys：让终端把修饰键单独上报（level 2 覆盖全部组合键）。
_MODIFY_OTHER_KEYS_ON = "\x1b[>4;2m"
_MODIFY_OTHER_KEYS_OFF = "\x1b[>4m"
_TERMINALS = frozenset({"iTerm.app", "WezTerm", "vscode", "ghostty"})


def is_shift_enter(data: Optional[str]) -> bool:
    return data in SHIFT_ENTER_SEQUENCES


def supports_modified_keys(env=None) -> bool:
    """只对已实现 modifyOtherKeys 的终端开启，避免在未知终端改写按键语义。

    kitty 不实现 modifyOtherKeys（只记一条错误日志），需要 Kitty 键盘协议，尚未接入。
    """
    env = os.environ if env is None else env
    if os.name == "nt":
        return False
    term_program = (env.get("TERM_PROGRAM") or "").strip()
    term = (env.get("TERM") or "").strip().lower()
    return bool(
        env.get("WT_SESSION")
        or term_program in _TERMINALS
        or term_program == "tmux"
        or term.startswith("tmux")
        or term == "xterm-ghostty"
    )


def tmux_extended_keys_hint(env=None) -> Optional[str]:
    """在 tmux 内且未开启 extended-keys 时返回一句修复提示；其余情况返回 None。

    默认配置的 tmux 会把 Shift+Enter 压成普通回车：aaa 这边开启了扩展键也没有用，
    用户只会看到「换行不灵」，所以在启动时点名告知怎么修。
    """
    env = os.environ if env is None else env
    in_tmux = env.get("TMUX") and (
        (env.get("TERM_PROGRAM") or "").strip() == "tmux"
        or (env.get("TERM") or "").strip().lower().startswith("tmux")
    )
    if not in_tmux:
        return None
    try:
        shown = subprocess.run(
            ["tmux", "show-options", "-s", "extended-keys"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if shown.strip().endswith("off"):
        return (
            "tmux 未开启 extended-keys，Shift+Enter 暂不可用；在 ~/.tmux.conf 加入"
            ' `set -s extended-keys on` 与 `set -as terminal-features "xterm*:extkeys"` 后重载。'
        )
    return None


@contextmanager
def extended_keys(stream=None):
    """会话期间开启 modifyOtherKeys，退出（含异常）恢复，避免污染父 shell。"""
    stream = sys.stdout if stream is None else stream
    if not (supports_modified_keys() and getattr(stream, "isatty", lambda: False)()):
        yield False
        return
    stream.write(_MODIFY_OTHER_KEYS_ON)
    stream.flush()
    try:
        yield True
    finally:
        stream.write(_MODIFY_OTHER_KEYS_OFF)
        stream.flush()


def _key(codepoint: int, modifier: int) -> Optional[str]:
    """把一次扩展按键还原成 prompt_toolkit 已绑定的传统字节；None 表示无法表示。"""
    bits = (max(modifier, 1) - 1) & 0b1111  # 去掉 CapsLock(64)/NumLock(128) 位
    if bits & 0b1000:  # Super/Meta/Hyper 没有对应键位，吞掉而不是当普通字符
        return None
    shift, alt, ctrl = bool(bits & 1), bool(bits & 2), bool(bits & 4)
    prefix = "\x1b" if alt else ""
    if codepoint in (8, 127):  # Backspace：Alt 前缀走 backward-kill-word
        return prefix + "\x7f"
    if codepoint == 13:
        return MARKER if bits else "\r"
    if codepoint == 9:
        return "\x1b[Z" if shift and not (ctrl or alt) else "\t"
    if codepoint == 32:
        return "\x00" if ctrl else " "
    if codepoint == 27:
        return "\x1b"
    if not 32 < codepoint < 127:
        return None
    character = chr(codepoint)
    if character.isalpha():
        if ctrl:
            body = chr(ord(character.lower()) & 0x1F)
        elif shift:
            body = character.upper()
        else:
            body = character
        return prefix + body
    if not ctrl:
        return prefix + character
    if 64 <= codepoint <= 95:  # Ctrl+@ [ \ ] ^ _ → C0 控制字节（Ctrl+[ 即 Esc）
        return prefix + chr(codepoint & 0x1F)
    return prefix + _CONTROL_DIGITS[codepoint] if codepoint in _CONTROL_DIGITS else None


def _decode(params: str, final: str) -> object:
    """返回替换字节、``None``（不是我们的序列，原样放行）或 ``DROPPED``（吞掉）。"""
    fields = params.split(";")
    if final == "~":
        # 只接管 xterm modifyOtherKeys 的 27;<mod>;<cp>~；其它 CSI-tilde 属于传统功能键。
        if len(fields) != 3 or fields[0] != "27":
            return None
        modifier, codepoint = fields[1], fields[2]
        event = ""
    elif final == "u":
        if len(fields) == 3 and fields[0] == "27":
            modifier, codepoint = fields[1], fields[2]
        elif len(fields) == 2:
            codepoint, modifier = fields[0], fields[1]
        elif len(fields) == 1:
            codepoint, modifier = fields[0], "1"
        else:
            return DROPPED  # CSI-u 一定是我们的序列，解析不了也不能漏成字面文本
        modifier, _, event = modifier.partition(":")
    else:
        return None
    if not (modifier.isdigit() and codepoint.isdigit()):
        return DROPPED if final == "u" else None
    if event == "3":  # 松开事件：吞掉，否则会多出一次换行
        return DROPPED
    replacement = _key(int(codepoint), int(modifier))
    return DROPPED if replacement is None else replacement


class _Dropped:
    __slots__ = ()


DROPPED = _Dropped()


class ModifiedEnter:
    """Streaming normalization; incomplete prefixes wait for input or Escape flush."""

    def __init__(self):
        self.pending = ""
        self.paste = False

    def _consume(self, text: str, start: int) -> tuple[int, Optional[str]]:
        """返回 ``(消耗字节, 替换字节)``；``0`` 表示等待更多输入，``-1`` 表示不是已知序列。"""
        tail = text[start:]
        for token in (_PASTE_START, _PASTE_END):
            if text.startswith(token, start):
                self.paste = token == _PASTE_START
                return len(token), token
        if len(tail) < _MAX_SEQUENCE and any(
            len(tail) < len(token) and token.startswith(tail) for token in _TOKENS
        ):
            return 0, None
        if self.paste:
            return -1, None
        for token in _ENTER_TOKENS:
            if text.startswith(token, start):
                return len(token), "" if token == _SHIFT_RELEASE else MARKER
        if not tail.startswith(_CSI):
            return -1, None
        end = 2
        while end < len(tail) and tail[end] in _PARAM_CHARS:
            end += 1
        if end == len(tail):
            return (0, None) if end <= _MAX_SEQUENCE else (-1, None)
        decoded = _decode(tail[2:end], tail[end])
        if decoded is None:
            return -1, None
        return end + 1, "" if decoded is DROPPED else decoded

    def feed(self, data: str) -> str:
        held = self.pending
        # 跨读取的裸 ESC 不参与 Alt+Enter 融合：Esc 后紧接着回车应保持「先取消、再提交」。
        cross = held == "\x1b" and data[:1] in ("\r", "\n")
        text, self.pending = ("" if cross else held) + data, ""
        out = ["\x1b"] if cross else []
        index = 0
        while index < len(text):
            escape = text.find("\x1b", index)
            if escape < 0:
                out.append(text[index:])
                break
            if escape > index:
                out.append(text[index:escape])
            consumed, replacement = self._consume(text, escape)
            if consumed == 0:
                self.pending = text[escape:]
                break
            if consumed < 0:
                out.append("\x1b")
                index = escape + 1
                continue
            out.append(replacement or "")
            index = escape + consumed
        return "".join(out)

    def flush(self) -> str:
        if self.paste:
            return ""
        text, self.pending = self.pending, ""
        return text


class _Reader:
    def __init__(self, reader):
        self.reader = reader
        self.modified_enter = ModifiedEnter()

    @property
    def closed(self):
        return self.reader.closed

    def read(self, *args, **kwargs):
        return self.modified_enter.feed(self.reader.read(*args, **kwargs))


if Vt100Input is not None:

    class ModifiedEnterInput(Vt100Input):
        """Keep native fd/TTY lifecycle; adapt this input's reader and flush only."""

        def __init__(self, stdin):
            super().__init__(stdin)
            self.stdin_reader = _Reader(self.stdin_reader)

        def flush_keys(self):
            self.vt100_parser.feed(self.stdin_reader.modified_enter.flush())
            return super().flush_keys()


def create_cli_input():
    return create_input() if os.name == "nt" else ModifiedEnterInput(sys.stdin)
