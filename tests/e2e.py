"""端到端测试：真实渠道、真实 CLI、真实工具闭环。

用法：
    .venv/bin/python tests/e2e.py --list
    .venv/bin/python tests/e2e.py            # P0 + P1
    .venv/bin/python tests/e2e.py --suite p0
    .venv/bin/python tests/e2e.py --pty      # 追加交互面（需 PTY）

约定：
- 每个用例独立 `HOME`，配置从真实 `~/.aaa/secret.yaml` 复制后按需改动；
  不写真实会话库。
- 默认保留环境代理（含 `ALL_PROXY`/`all_proxy`），以匹配用户真实网络；
  代理不可用或只想直连时加 `--no-proxy`。
- 断言只依赖 CLI 的可观察输出、产物文件与 `~/.aaa/logs/aaa.log`。
- 结果三态：PASS / FAIL / SKIP（含原因）。任一 FAIL 退出码 1。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
REAL_SECRET = Path.home() / ".aaa" / "secret.yaml"
PYTHON = os.environ.get("AAA_PYTHON", sys.executable)
STRIP_PROXY = False

RESULTS: list[tuple[str, str, str]] = []


# ---------------------------------------------------------------- 基础设施


def token(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def load_base_config() -> dict:
    import yaml

    return yaml.safe_load(REAL_SECRET.read_text(encoding="utf-8"))


def make_home(config: dict | None = None) -> Path:
    import yaml

    home = Path(tempfile.mkdtemp(prefix="aaa-e2e-"))
    (home / ".aaa").mkdir(parents=True, exist_ok=True)
    cfg = config if config is not None else load_base_config()
    secret = home / ".aaa" / "secret.yaml"
    secret.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(secret, 0o600)
    return home


def run_cli(args: list[str], home: Path, cwd: Path, timeout: int = 240) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("ALL_PROXY", None)
    env.pop("all_proxy", None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("AAA_LOG_LEVEL", "INFO")
    proc = subprocess.run(
        [PYTHON, "-m", "aaa", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def log_text(home: Path) -> str:
    path = home / ".aaa" / "logs" / "aaa.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _db_contains(home: Path, needle: str) -> bool:
    """会话库里是否出现某段文本（用于断言卸载信封与回读调用）。"""
    import sqlite3

    db = home / ".aaa" / "data" / "sessions.db"
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                rows = conn.execute(f'SELECT * FROM "{table}"')
            except sqlite3.Error:
                continue
            for row in rows:
                for value in row:
                    if isinstance(value, str) and needle in value:
                        return True
                    if isinstance(value, bytes) and needle.encode() in value:
                        return True
    finally:
        conn.close()
    return False


def seed_legacy_session(home: Path, work: Path) -> str:
    from agno.db.sqlite import SqliteDb
    from agno.session import AgentSession

    data = home / ".aaa" / "data"
    data.mkdir(exist_ok=True)
    db = SqliteDb(db_file=str(data / "sessions.db"))
    session_id = f"legacy-{token()}"
    try:
        db.upsert_session(AgentSession(session_id=session_id, user_id="default", metadata={"cwd": str(work)}))
    finally:
        db.db_engine.dispose()
    return session_id


def record(name: str, status: str, detail: str) -> None:
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name} — {detail}")


def case(name: str):
    def wrap(fn):
        def inner() -> None:
            try:
                status, detail = fn()
            except Exception as exc:  # 用例自身异常按失败记录
                record(name, "FAIL", f"{type(exc).__name__}: {exc}")
                return
            record(name, status, detail)

        inner.case_name = name  # type: ignore[attr-defined]
        return inner

    return wrap


# ---------------------------------------------------------------- P0 闭环


@case("P0.1 真实工具调用（read-only）")
def p01_tool_read():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = token()
    (work / "data.txt").write_text(f"{mark}\n", encoding="utf-8")
    code, out, err = run_cli(
        ["-p", "读取 data.txt，只回复文件内容，不要解释"], home, work
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    if mark not in out:
        return "FAIL", f"输出未包含标记 {mark}：{out.strip()[:200]}"
    return "PASS", "工具读取命中标记，exit=0"


@case("P0.2 真实写入（danger-full-access）")
def p02_write():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = token()
    code, out, err = run_cli(
        ["--permission", "danger-full-access", "-p",
         f"把 {mark} 写入 {work / 'out.txt'}，只回复 ok"],
        home,
        work,
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    target = work / "out.txt"
    if not target.exists():
        return "FAIL", "out.txt 未创建"
    if mark not in target.read_text(encoding="utf-8"):
        return "FAIL", "out.txt 内容不含标记"
    return "PASS", "文件已写入且内容正确"


@case("P0.3 非交互拒绝 shell（workspace-write）")
def p03_reject():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = token()
    # 只有 run_shell_command 标记了 requires_confirmation；文件写入不需要审批。
    code, out, err = run_cli(
        ["--permission", "workspace-write", "-p", f"运行 echo {mark} 并告诉我输出"],
        home,
        work,
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    if "非交互模式无法确认工具" not in log_text(home):
        return "FAIL", "日志里没有拒绝记录"
    if "run_shell_command" not in log_text(home):
        return "FAIL", "拒绝记录里没有工具名"
    return "PASS", "shell 调用被拒绝并记录，进程正常结束"


@case("P0.4 多轮续接（-r）")
def p04_resume():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = token()
    code, out, _ = run_cli(["--mode", "json", f"记住这个串：{mark}。只回复收到"], home, work)
    if code != 0:
        return "FAIL", f"第一轮 exit={code}"
    session_id = None
    for line in out.strip().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            session_id = event.get("session_id")
    if not session_id:
        return "FAIL", "未取到 session_id"
    code, out2, err = run_cli(["-r", session_id, "-p", "刚才我让你记住的串是什么？只回答串"], home, work)
    if code != 0:
        return "FAIL", f"续接 exit={code} stderr={err.strip()[:200]}"
    if mark not in out2:
        return "FAIL", f"续接后未答出标记：{out2.strip()[:200]}"
    return "PASS", f"session={session_id} 续接答对"


@case("P0.5 JSON 事件流")
def p05_json_events():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    (work / "a.txt").write_text("hi\n", encoding="utf-8")
    code, out, _ = run_cli(["--mode", "json", "列出当前目录的文件"], home, work)
    if code != 0:
        return "FAIL", f"exit={code}"
    kinds, result = set(), None
    for line in out.strip().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return "FAIL", f"非 JSON 行：{line[:80]}"
        kinds.add(event.get("type"))
        if event.get("type") == "result":
            result = event
    if "tool_start" not in kinds:
        return "FAIL", f"缺少工具事件：{sorted(kinds)}"
    if not result or result.get("session_id") is None:
        return "FAIL", "缺少 result 行或 session_id"
    if not result.get("tokens"):
        return "FAIL", "result.tokens 为空"
    return "PASS", f"事件={sorted(kinds)} tokens={result.get('tokens')}"


@case("P0.6 错误路径（坏 key）")
def p06_bad_key():
    cfg = load_base_config()
    cfg["providers"]["tokenhub"]["api_key"] = "invalid-key"
    home, work = make_home(cfg), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    code, out, err = run_cli(["-p", "只回复 ok"], home, work, timeout=180)
    if code == 0:
        return "FAIL", "坏 key 仍然 exit=0"
    if "ERROR" not in log_text(home):
        return "FAIL", f"日志无 ERROR 记录，stderr={err.strip()[:200]}"
    return "PASS", f"exit={code}，日志有 ERROR"


@case("P0.7 损坏配置不被覆盖")
def p07_corrupt_config():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    secret = home / ".aaa" / "secret.yaml"
    secret.write_text("providers:\n  tokenhub:\n    base_url: {broken\n", encoding="utf-8")
    before = secret.read_bytes()
    code, out, err = run_cli(["-p", "只回复 ok"], home, work, timeout=60)
    if code != 2:
        return "FAIL", f"损坏配置 exit={code}，期望 2（配置错误）"
    if secret.read_bytes() != before:
        return "FAIL", "配置文件被改写"
    if "无法解析" not in err and "无法解析" not in out and "不是合法 YAML" not in (err + out):
        return "FAIL", f"未给出解析错误提示：{(err or out).strip()[:200]}"
    if str(secret) not in (err + out):
        return "FAIL", "错误提示里没有配置文件路径"
    return "PASS", "exit=2，提示解析错误，文件未被覆盖"


@case("P0.8 --allow-shell 放行命令")
def p08_allow_shell():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = token()
    code, out, err = run_cli(
        ["--permission", "workspace-write", "--allow-shell", "-p",
         f"运行 echo {mark} 并原样告诉我输出"],
        home,
        work,
        timeout=180,
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    if mark not in out:
        return "FAIL", f"输出未包含命令结果：{out.strip()[:200]}"
    if "已拒绝" in log_text(home):
        return "FAIL", "日志里出现拒绝记录，放行未生效"
    return "PASS", "命令执行且结果回填"


@case("P0.9 grep 工具（只读）")
def p09_grep_tool():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("def build_agent():\n    pass\n", encoding="utf-8")
    code, out, err = run_cli(
        ["-p", "用 grep 工具找出 build_agent 定义在哪个文件，只回答相对路径"],
        home,
        work,
        timeout=180,
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    if "src/app.py" not in out:
        return "FAIL", f"未定位到文件：{out.strip()[:200]}"
    return "PASS", "正则检索定位到 src/app.py"


@case("P0.10 大结果卸载可回读")
def p10_offload():
    home, work = make_home(), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    mark = f"LASTLINE-{token()}"
    lines = [f"line {i:04d} " + "x" * 40 for i in range(1, 1500)]
    lines.append(mark)
    (work / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out, err = run_cli(
        ["-p", "读取 big.txt，告诉我最后一行；如果内容被存到别处，用 read_result 取回"],
        home,
        work,
        timeout=240,
    )
    if code != 0:
        return "FAIL", f"exit={code} stderr={err.strip()[:200]}"
    if mark not in out:
        return "FAIL", f"未答出最后一行：{out.strip()[:200]}"
    if not _db_contains(home, "<result id="):
        return "FAIL", "会话库里没有卸载信封，结果未被卸载"
    if not _db_contains(home, "read_result"):
        return "FAIL", "会话库里没有 read_result 调用记录"
    return "PASS", "大结果被卸载且经 read_result 取回"


# ---------------------------------------------------------------- P1 治理


@case("P1.7 压缩触发")
def p07_compaction():
    cfg = load_base_config()
    for model in cfg["providers"]["tokenhub"]["models"]:
        model["context_window"] = 20000  # 阈值 = 20000 − 16384 = 3616，压低成本
    home, work = make_home(cfg), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    session_id = seed_legacy_session(home, work)
    for round_index in range(10):
        args = ["-r", session_id, "-p"]
        args.append(f"输出 60 行内容，每行带编号和随机字母，不要解释（第{round_index + 1}轮）")
        code, out, err = run_cli(args, home, work, timeout=300)
        if code != 0:
            return "FAIL", f"第 {round_index + 1} 轮 exit={code} stderr={err.strip()[:200]}"
        if "Auto-compacting" in log_text(home):
            return "PASS", f"第 {round_index + 1} 轮触发自动压缩"
    return "FAIL", "6 轮后仍未触发压缩"


@case("P1.8 归档检索（search_compacted_history）")
def p08_archive_search():
    cfg = load_base_config()
    for model in cfg["providers"]["tokenhub"]["models"]:
        model["context_window"] = 20000
    home, work = make_home(cfg), Path(tempfile.mkdtemp(prefix="aaa-ws-"))
    session_id = seed_legacy_session(home, work)
    mark = token(10)
    run_cli(["-r", session_id, "-p", f"记住这个标识：{mark}。只回复收到"], home, work)
    for round_index in range(12):
        code, _, _ = run_cli(
            ["-c", "-p", f"输出 60 行内容，每行带编号和随机字母，不要解释（第{round_index + 1}轮）"],
            home,
            work,
            timeout=300,
        )
        if code != 0:
            break
        if "Auto-compacting" in log_text(home):
            break
    if "Auto-compacting" not in log_text(home):
        return "SKIP", "未能先触发压缩，检索路径不可达"
    code, out, _ = run_cli(
        ["-c", "-p", "最早那轮我让你记住的标识是什么？需要时检索被压缩的历史。只回答标识"],
        home,
        work,
        timeout=300,
    )
    if code != 0:
        return "FAIL", f"检索轮 exit={code}"
    if mark not in out:
        return "FAIL", f"未找回标识：{out.strip()[:200]}"
    if "search_compacted_history" not in log_text(home):
        return "PASS", "答对但未走检索工具（可能仍在保留尾内）"
    return "PASS", "经 search_compacted_history 找回"


@case("P0.11 按 ID 续聊已有 Agent 会话")
def p11_resume_saved_agent():
    with tempfile.TemporaryDirectory(prefix="aaa-agent-resume-") as folder:
        work = Path(folder)
        home = make_home()
        session_id = seed_legacy_session(home, work)
        mark = token()
        code, out, _ = run_cli(["-r", session_id, "--mode", "json", f"只回复 {mark}"], home, work)
        if code != 0 or mark not in out or session_id not in out:
            return "FAIL", "未使用既有 Agent 会话完成运行"
        return "PASS", "原会话 ID 未改写"



# ---------------------------------------------------------------- P2 交互（可选）

# P2 由 tests/pty_cases.py 提供（PTY 驱动），--pty 时导入。


# ---------------------------------------------------------------- 入口


CASES = [
    ("p0", [p01_tool_read, p02_write, p03_reject, p04_resume, p05_json_events, p06_bad_key,
            p07_corrupt_config, p08_allow_shell, p09_grep_tool, p10_offload, p11_resume_saved_agent]),
    ("p1", [p07_compaction, p08_archive_search]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="aaa 端到端测试")
    parser.add_argument("--suite", choices=["p0", "p1", "all"], default="all")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--pty", action="store_true", help="追加交互面用例（P2）")
    parser.add_argument("--only", default=None, help="只跑用例名包含该子串的用例")
    parser.add_argument("--no-proxy", action="store_true", help="剔除 ALL_PROXY/all_proxy（代理不可用时）")
    args = parser.parse_args()
    global STRIP_PROXY
    STRIP_PROXY = bool(args.no_proxy)

    if args.list:
        for suite, cases in CASES:
            for fn in cases:
                print(f"{suite}\t{fn.case_name}")
        print("p2\tP2.11 HITL 真实确认（PTY）")
        return 0

    if not REAL_SECRET.exists():
        print(f"缺少 {REAL_SECRET}，先配置渠道", file=sys.stderr)
        return 2

    # P1.7/P1.8 靠轮数压到阈值，依赖准确计数；缺 tiktoken 时退回 chars/4
    # 估算（实测低估约 3.2×），阈值要到更晚轮次，用例会误报失败。
    if args.suite in ("p1", "all"):
        try:
            import tiktoken  # noqa: F401
        except ImportError:
            print("缺少 tiktoken：P1 的压缩用例依赖本地 token 计数，先安装再跑", file=sys.stderr)
            return 2

    suites = [suite for suite, _ in CASES] if args.suite == "all" else [args.suite]
    for suite in suites:
        for fn in dict(CASES)[suite]:
            if args.only and args.only not in fn.case_name:
                continue
            print(f"--- {fn.case_name}")
            fn()
    if args.pty:
        from pty_cases import hitl_case, wizard_case

        selected = args.only or ""
        if not selected or "P2.11" in selected:
            print("--- P2.11 HITL 真实确认（PTY）")
            hitl_case(record)
        if not selected or "P2.12" in selected:
            print("--- P2.12 /provide 真实向导（PTY）")
            wizard_case(record, load_base_config()["providers"]["deepseek"]["api_key"])

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print("\n=== 汇总 ===")
    for name, status, detail in RESULTS:
        print(f"{status:4} {name} — {detail}")
    print(f"PASS {sum(1 for r in RESULTS if r[1] == 'PASS')} / "
          f"FAIL {len(failed)} / SKIP {sum(1 for r in RESULTS if r[1] == 'SKIP')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
