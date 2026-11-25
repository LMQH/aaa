"""受管进程：前台执行带超时，后台任务在进程内记录、由模型轮询。

后台状态只活在当前进程：进程退出时未结束的 job 一律终止，也没有磁盘状态可让
下一次 `-p` 调用接着查。跨进程持久属于运行期契约的变更（`docs/architecture.md`），
不能靠顺手写几个文件引入。
"""

import atexit
import locale
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from agno.tools.shell import ShellTools

# 与前台保持一致：前台用 Popen(text=True) 按区域编码解码，后台也按同一编码解。
_ENCODING = locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
else:
    import signal

# Windows 进程创建标志：独立进程组便于整树操作；不要弹出控制台窗口。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
# Job Object：句柄关闭时杀掉作业内所有进程（含后代）。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


def _join_argv(args: List[str]) -> str:
    return subprocess.list2cmdline(args) if _IS_WINDOWS else shlex.join(args)


if _IS_WINDOWS:

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _kernel32():
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        api.OpenProcess.restype = wintypes.HANDLE
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.CloseHandle.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        return api

    class _WindowsJob:
        """一个后台任务的 Job Object：终止或关闭句柄都会带走整棵进程树。"""

        def __init__(self, pid: int) -> None:
            self._api = _kernel32()
            self.handle = self._api.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")
            try:
                info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not self._api.SetInformationJobObject(
                    self.handle,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ):
                    raise OSError(ctypes.get_last_error(), "SetInformationJobObject 失败")
                process = self._api.OpenProcess(
                    _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid
                )
                if not process:
                    raise OSError(ctypes.get_last_error(), "OpenProcess 失败")
                try:
                    if not self._api.AssignProcessToJobObject(self.handle, process):
                        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject 失败")
                finally:
                    self._api.CloseHandle(process)
            except OSError:
                self.close()
                raise

        def kill(self) -> None:
            if self.handle:
                self._api.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            if self.handle:
                self._api.CloseHandle(self.handle)
                self.handle = None

    def _kill_tree(pid: int) -> None:
        """没有 Job Object 时的兜底：taskkill 连带子进程一起杀。"""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass

# 前台命令超时：超过即杀整个进程组。长任务用 background=True。
TIMEOUT_SECONDS = 120
# 单次返回给模型的输出上限。要高于 Agno 卸载阈值（16k 字符），
# 让超限结果以“信封 + 可回读全文”的形态进上下文，而不是在工具层被截断。
MAX_OUTPUT_BYTES = 1_000_000
# 已结束 job 的保留条数：每条最多占 max_output_bytes 内存，超出即丢最旧的。
MAX_FINISHED_JOBS = 20
# 读线程每次从管道取的字节数。
READ_CHUNK = 8192


class _Job:
    """一个后台任务的进程内状态：进程、整树句柄、输出缓冲与退出码。"""

    __slots__ = (
        "id", "argv", "process", "tree", "started_at", "exit_code", "thread", "buffer", "lock",
    )

    def __init__(self, job_id: str, argv: List[str], process: subprocess.Popen) -> None:
        self.id = job_id
        self.argv = argv
        self.process = process
        self.tree = None  # Windows：Job Object；POSIX 靠进程组，不需要
        self.started_at = int(time.time())
        self.exit_code: Optional[int] = None
        self.thread: Optional[threading.Thread] = None
        self.buffer = bytearray()
        self.lock = threading.Lock()


class ProcessTools(ShellTools):
    """受管进程：前台带超时与进程组回收；后台任务记在进程内，由模型轮询。"""

    TIMEOUT_SECONDS = TIMEOUT_SECONDS
    MAX_OUTPUT_BYTES = MAX_OUTPUT_BYTES

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        timeout_seconds: Optional[int] = None,
        max_output_bytes: Optional[int] = None,
        default_tail: Optional[int] = None,
        **kwargs,
    ) -> None:
        super().__init__(base_dir=base_dir, **kwargs)
        self.timeout_seconds = int(timeout_seconds or TIMEOUT_SECONDS)
        self.max_output_bytes = int(max_output_bytes or MAX_OUTPUT_BYTES)
        self.default_tail = int(default_tail or 100)
        self._jobs: Dict[str, _Job] = {}
        # run_shell_command 由父类注册；后台轮询的入口在这里补上。
        self.register(self.job_output)
        self.register(self.job_kill)
        atexit.register(self._shutdown)

    # ------------------------------------------------------------ 前台

    def run_shell_command(self, args: List[str], tail: Optional[int] = None, background: bool = False) -> str:
        """Runs a shell command and returns the output or error.

        Args:
            args: The command to run as a list of strings.
            tail: The number of lines to return from the output.
            background: Start the command as a background job and return its job id; poll with job_output.
        """
        lines = int(tail) if tail else self.default_tail
        if background:
            return self._start_job(args)
        cwd = str(self.base_dir) if self.base_dir else None
        # POSIX 建新会话、按进程组回收；Windows 建独立进程组并隐藏窗口，超时走整树终止。
        platform_args = (
            {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
            if _IS_WINDOWS
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                **platform_args,
            )
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                if _IS_WINDOWS:
                    _kill_tree(process.pid)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return (
                    f"Error: timeout after {self.timeout_seconds}s. "
                    "For long-running commands, call again with background=true and poll job_output."
                )
        except Exception as exc:
            return f"Error: {exc}"

        if process.returncode == 0:
            output = stdout
        else:
            output = (stderr or stdout).strip() or f"Error: exit code {process.returncode}"
        if len(output) > self.max_output_bytes:
            output = output[: self.max_output_bytes] + "\n[output truncated]"
        return "\n".join(output.split("\n")[-lines:])

    # ------------------------------------------------------------ 后台

    def _start_job(self, args: List[str]) -> str:
        """起一个后台进程，返回 job id；长任务靠 job_output 轮询。"""
        job_id = uuid.uuid4().hex[:8]
        cwd = str(self.base_dir) if self.base_dir else None
        platform_args = (
            {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
            if _IS_WINDOWS
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                **platform_args,
            )
        except Exception as exc:
            return f"Error: {exc}"

        job = _Job(job_id, list(args), process)
        if _IS_WINDOWS:
            try:
                job.tree = _WindowsJob(process.pid)
            except OSError:
                job.tree = None  # 建不起来就退回 taskkill 兜底，任务照常可用
        reader = threading.Thread(target=self._pump, args=(job,), daemon=True)
        job.thread = reader
        self._jobs[job_id] = job
        self._evict_finished()
        reader.start()
        return (
            f"job {job_id} started: {_join_argv(args)}\n"
            f"poll with job_output(job_id=\"{job_id}\"), stop with job_kill(job_id=\"{job_id}\")"
        )

    def job_output(self, job_id: str, tail: Optional[int] = None) -> str:
        """Read a background job's status and recent output.

        Args:
            job_id: The id returned by run_shell_command(background=true).
            tail: The number of trailing output lines to return.
        """
        job = self._job(job_id)
        if job is None:
            return f"Error: 未知 job id：{job_id}"

        status, code = self._status(job)
        header = f"job {job_id} · {status}"
        if code is not None:
            header += f" · exit {code}"
        output = "\n".join(self._text(job).split("\n")[-int(tail or self.default_tail):])
        return f"{header}\n{output}" if output else header

    def job_kill(self, job_id: str) -> str:
        """Stop a running background job, killing its whole process tree.

        Args:
            job_id: The id returned by run_shell_command(background=true).
        """
        job = self._job(job_id)
        if job is None:
            return f"Error: 未知 job id：{job_id}"
        status, code = self._status(job)
        if status != "running":
            return f"job {job_id} 已结束（exit {code}），无需终止"
        try:
            self._kill(job)
        except ProcessLookupError:
            return f"job {job_id} 的进程已不存在"
        except Exception as exc:
            return f"Error: {exc}"
        return f"job {job_id} 已终止"

    # ------------------------------------------------------------ 内部

    def _kill(self, job: _Job) -> None:
        """终止整棵进程树：Windows 用 Job Object（失败退回 taskkill），POSIX 杀进程组。"""
        if _IS_WINDOWS:
            if job.tree is not None:
                job.tree.kill()
            else:
                _kill_tree(job.process.pid)
            return
        os.killpg(job.process.pid, signal.SIGKILL)

    def _pump(self, job: _Job) -> None:
        """把子进程输出读进有界缓冲；只保留最后 `max_output_bytes` 字节。"""
        stream = job.process.stdout
        if stream is None:
            return
        try:
            while True:
                try:
                    chunk = stream.read1(READ_CHUNK)
                except (ValueError, OSError):
                    break
                if not chunk:
                    break
                with job.lock:
                    job.buffer.extend(chunk)
                    overflow = len(job.buffer) - self.max_output_bytes
                    if overflow > 0:
                        del job.buffer[:overflow]
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _job(self, job_id: str) -> Optional[_Job]:
        if not isinstance(job_id, str):
            return None
        return self._jobs.get(job_id)

    def _status(self, job: _Job) -> tuple:
        code = job.process.poll()
        if code is None:
            return "running", None
        if job.exit_code is None:
            job.exit_code = code
            # 进程已退出，等读线程把管道里剩下的输出取完再返回，避免尾部截断。
            if job.thread is not None and job.thread.is_alive():
                job.thread.join(timeout=1.0)
        return "finished", job.exit_code

    def _text(self, job: _Job) -> str:
        with job.lock:
            return bytes(job.buffer).decode(_ENCODING, "replace")

    def _evict_finished(self) -> None:
        finished = [
            job.id for job in self._jobs.values()
            if job.process.poll() is not None
        ]
        for job_id in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            job = self._jobs.pop(job_id, None)
            if job is not None and job.tree is not None:
                job.tree.close()

    def _shutdown(self) -> None:
        """进程退出时终止仍在本进程名下的 job，避免留下孤儿进程。"""
        for job in list(self._jobs.values()):
            if job.process.poll() is not None:
                if job.tree is not None:
                    job.tree.close()
                continue
            try:
                self._kill(job)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                job.process.stdout.close()
            except (AttributeError, OSError):
                pass
