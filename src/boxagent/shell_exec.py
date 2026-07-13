"""shell_exec — 共享的 shell 命令执行核心。

`/exec` slash 命令（`router/commands/tools.py`）与 `POST /api/exec` web 端点
（`transports/web/server.py`）共用同一份实现：pwsh(Windows)/shell(Unix) 拉起、
超时杀进程树、strip ANSI。leaf util，不依赖 router/web 任何一层。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

EXEC_DEFAULT_TIMEOUT = 30
EXEC_MAX_TIMEOUT = 600

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class ShellResult:
    """一次 shell 执行的结果。timed_out=True 时 exit_code 无意义。"""

    exit_code: int | None
    output: str
    timed_out: bool = False


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其子孙——Unix 走进程组，Windows 走 taskkill /T。"""
    if process.returncode is not None:
        return
    pid = process.pid
    if sys.platform == "win32":
        import subprocess
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def clamp_timeout(timeout: int) -> int:
    """把超时夹到 [1, EXEC_MAX_TIMEOUT]。"""
    return max(1, min(int(timeout), EXEC_MAX_TIMEOUT))


async def run_shell_command(
    command: str, *, workspace: str | None = None, timeout: int = EXEC_DEFAULT_TIMEOUT,
) -> ShellResult:
    """在 `workspace`（不存在则用进程 cwd）里跑一条 shell 命令，最多等 `timeout` 秒。

    正常返回 `ShellResult(exit_code, output)`；超时杀进程树后返回
    `ShellResult(None, "", timed_out=True)`。拉起失败的异常向上抛，由调用方处理。
    """
    cwd = workspace if workspace and Path(workspace).is_dir() else None

    shell_args: list[str] | None = None
    shell_env: dict[str, str] | None = None
    if sys.platform == "win32":
        pwsh = (
            shutil.which("pwsh") or shutil.which("pwsh.exe")
            or shutil.which("powershell") or shutil.which("powershell.exe")
        )
        if pwsh:
            shell_args = [pwsh, "-NoProfile", "-NoLogo", "-Command", command]
            shell_env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}

    if shell_args:
        process = await asyncio.create_subprocess_exec(
            *shell_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=shell_env,
            start_new_session=True,
        )
    else:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_tree(process)
        await process.wait()
        return ShellResult(exit_code=None, output="", timed_out=True)

    output = _ANSI_RE.sub("", stdout.decode("utf-8", errors="replace").rstrip())
    return ShellResult(exit_code=process.returncode, output=output, timed_out=False)
