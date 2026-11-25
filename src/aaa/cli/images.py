"""终端内图片显示：能力探测、两种图形协议编码与文本降级。

只做「把本地图片显示在终端里」：不支持图形协议的终端退回一行文本标签，
不写任何图片数据。探测基于环境变量（不做阻塞式查询），覆盖 iTerm2/WezTerm
（OSC 1337）与 kitty/Ghostty（kitty graphics）；tmux 内不尝试透传，直接降级。
"""

import base64
import os
import struct
from pathlib import Path
from typing import Callable, List, Optional

SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

ITERM2 = "iterm2"
KITTY = "kitty"

_KITTY_CHUNK = 4096


def detect_protocol(env: Optional[dict] = None) -> Optional[str]:
    """按环境变量判定可用的图形协议；未知或已知不支持时返回 None。"""
    env = os.environ if env is None else env
    if env.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm"):
        return ITERM2
    if env.get("KITTY_WINDOW_ID") or env.get("TERM", "").endswith("-kitty"):
        return KITTY
    if env.get("TERM_PROGRAM") == "ghostty" or env.get("TERM") == "xterm-ghostty":
        return KITTY
    return None


def dimensions(data: bytes) -> Optional[tuple]:
    """PNG / GIF / JPEG 的宽高；解析不了返回 None。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return int(width), int(height)
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
            index += 2 + length
    return None


def label(path: Path, data: bytes) -> str:
    """文本降级标签：`[图片] 名称 800x600 123KB`。"""
    size = f"{max(1, round(len(data) / 1024))}KB"
    dim = dimensions(data)
    shape = f" {dim[0]}x{dim[1]}" if dim else ""
    return f"[图片] {path.name}{shape} {size}"


def encode_iterm2(data: bytes, name: str, columns: int) -> str:
    """iTerm2/WezTerm 内联图片（OSC 1337）。"""
    body = base64.b64encode(data).decode("ascii")
    encoded_name = base64.b64encode(name.encode("utf-8")).decode("ascii")
    return (
        f"\x1b]1337;File=inline=1;size={len(data)};name={encoded_name};"
        f"width={max(1, columns)};preserveAspectRatio=1:{body}\x07"
    )


def encode_kitty(data: bytes, columns: int) -> str:
    """kitty graphics 协议：直接传输并显示，base64 分块。"""
    body = base64.b64encode(data).decode("ascii")
    chunks = [body[i:i + _KITTY_CHUNK] for i in range(0, len(body), _KITTY_CHUNK)] or [""]
    out: List[str] = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        head = f"a=T,f=100,q=2,c={max(1, columns)},m={more}"
        out.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(out)


def render(
    path: Path,
    *,
    writer: Callable[[str], None],
    columns: int,
    protocol: Optional[str] = None,
    env: Optional[dict] = None,
) -> bool:
    """把图片写入终端；成功返回 True，协议不可用或失败返回 False（由调用方降级）。"""
    protocol = protocol or detect_protocol(env)
    if protocol is None:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if protocol == ITERM2:
        writer(encode_iterm2(data, path.name, columns))
    else:
        writer(encode_kitty(data, columns))
    return True


def find_image_paths(text: str, *, max_bytes: int = 0, exists: Optional[Callable[[Path], bool]] = None) -> List[Path]:
    """从文本里挑出「整行就是一个存在的图片路径」的行。

    只认整行路径，避免把 JSON、日志里意外出现的路径当作图片渲染。
    """
    exists = exists or (lambda p: p.is_file())
    found: List[Path] = []
    for line in str(text or "").splitlines():
        candidate = line.strip().strip("`'\"")
        if not candidate or " " in candidate:
            continue
        path = Path(candidate).expanduser()
        if path.suffix.lower() not in SUFFIXES or not exists(path):
            continue
        if max_bytes > 0:
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
        if path not in found:
            found.append(path)
    return found
