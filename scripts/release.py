#!/usr/bin/env python3
"""aaa 发布工具。

非交互：调用者必须先取得 owner 对版本号的明确确认（AGENTS.md）。
CI 永不创建 tag；tag 只能由本脚本的 tag 子命令创建并推送。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


def fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env={**os.environ, **(env or {})},
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError:
        fail(f"找不到命令：{args[0]}")
    if result.returncode != 0:
        if capture:
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        fail(f"命令失败：{' '.join(args)}")
    return (result.stdout or "").strip()


def git(*args: str) -> str:
    return run(["git", *args], capture=True)


def git_ok(*args: str) -> bool:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def normalize_tag(value: str) -> str:
    tag = value.strip()
    if not TAG_RE.fullmatch(tag):
        fail(f"版本号必须形如 vX.Y.Z：{value}")
    if not tag.startswith("v"):
        tag = f"v{tag}"
    if tag == "v0.0.0":
        fail("v0.0.0 不是可发布版本")
    return tag


def has_remote() -> bool:
    return git_ok("remote", "get-url", "origin")


def preflight(tag: str) -> None:
    print(f"preflight {tag}")
    if git("status", "--porcelain"):
        fail("工作区不干净；先提交或清理")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        fail(f"当前分支是 {branch}，发布必须在 main")
    if git_ok("rev-parse", "-q", "--verify", f"refs/tags/{tag}"):
        fail(f"tag 已存在：{tag}")

    if has_remote():
        run(["git", "fetch", "--quiet", "origin", "main"], capture=True)
        if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
            fail("HEAD 与 origin/main 不一致；先同步")
        remote_tag = git("ls-remote", "--tags", "origin", f"refs/tags/{tag}")
        if remote_tag:
            fail(f"远程 tag 已存在：{tag}")

    print("  同步依赖")
    run(["uv", "sync", "--frozen", "--extra", "embedding"])
    print("  离线回归")
    run(["./tests/run.sh"])
    print("preflight 通过")


def create_tag(tag: str) -> None:
    preflight(tag)

    if has_remote():
        print("推送 main")
        run(["git", "push", "origin", "main"])

    print(f"创建 annotated tag {tag}")
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"])

    if not has_remote():
        print(f"本地 tag 已创建：{tag}；未配置 origin，暂不推送")
        return

    print(f"推送 {tag}")
    try:
        run(["git", "push", "origin", tag], env={"AAA_ALLOW_TAG_PUSH": tag})
    except SystemExit:
        print(f"tag {tag} 已在本地创建，但推送失败；修复网络后运行：", file=sys.stderr)
        print(f"  AAA_ALLOW_TAG_PUSH={tag} git push origin {tag}", file=sys.stderr)
        raise
    print(f"已推送 {tag}；CI 只在收到 tag 后构建，不创建 tag")


def verify(tag: str) -> None:
    dist = ROOT / "dist"
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    sums = dist / "SHA256SUMS"
    if not wheels or not sdists:
        fail("dist/ 缺少 wheel 或 sdist；先 uv build")
    if not sums.is_file():
        fail("dist/SHA256SUMS 不存在")

    entries: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            fail(f"SHA256SUMS 格式错误：{line}")
        digest, name = parts
        entries[name] = digest

    for path in [*wheels, *sdists]:
        expected = entries.get(path.name)
        if expected is None:
            fail(f"SHA256SUMS 缺少 {path.name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"{path.name} 校验失败")

    print(f"本地产物校验通过：{', '.join(p.name for p in [*wheels, *sdists])}")

    if shutil.which("gh") and has_remote():
        print("检查 GitHub Release 资产")
        try:
            out = run(
                ["gh", "release", "view", tag, "--json", "assets,isDraft"],
                capture=True,
            )
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - 外部命令异常
            fail(f"gh 校验失败：{exc}")
        data = json.loads(out)
        names = {asset["name"] for asset in data.get("assets", [])}
        expected = {p.name for p in [*wheels, *sdists]} | {"install.sh", "SHA256SUMS"}
        missing = expected - names
        if missing:
            fail(f"Release 缺少资产：{', '.join(sorted(missing))}")
        print(f"Release 资产齐全（draft={data.get('isDraft')}）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="aaa 发布工具；非交互，调用前必须取得 owner 对版本号的确认",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="只做发布前检查，不改仓库")
    pre.add_argument("tag")
    tag = sub.add_parser("tag", help="preflight 后创建并推送 annotated tag")
    tag.add_argument("tag")
    verify_parser = sub.add_parser("verify", help="校验本地产物与 Release 资产")
    verify_parser.add_argument("tag")

    args = parser.parse_args()
    normalized = normalize_tag(args.tag)
    if args.command == "preflight":
        preflight(normalized)
    elif args.command == "tag":
        create_tag(normalized)
    else:
        verify(normalized)


if __name__ == "__main__":
    main()
