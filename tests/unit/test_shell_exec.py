"""Tests for boxagent.shell_exec.run_shell_command."""
from __future__ import annotations

import sys

import pytest

from boxagent.shell_exec import (
    EXEC_MAX_TIMEOUT,
    clamp_timeout,
    run_shell_command,
)

# 这些用例跑真 shell，命令是 POSIX 语法——Windows 上跳过。
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


async def test_echo_returns_zero_and_output(tmp_path):
    result = await run_shell_command("echo hello", workspace=str(tmp_path), timeout=10)
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.output == "hello"


async def test_nonzero_exit_code_captured(tmp_path):
    result = await run_shell_command("exit 3", workspace=str(tmp_path), timeout=10)
    assert result.timed_out is False
    assert result.exit_code == 3


async def test_stderr_merged_into_output(tmp_path):
    result = await run_shell_command("echo oops 1>&2", workspace=str(tmp_path), timeout=10)
    assert result.exit_code == 0
    assert "oops" in result.output


async def test_timeout_kills_and_flags(tmp_path):
    result = await run_shell_command("sleep 5", workspace=str(tmp_path), timeout=1)
    assert result.timed_out is True
    assert result.exit_code is None


async def test_missing_workspace_falls_back_to_cwd(tmp_path):
    # 不存在的目录 → 落进程 cwd，不报错
    result = await run_shell_command("echo ok", workspace=str(tmp_path / "nope"), timeout=10)
    assert result.exit_code == 0
    assert result.output == "ok"


def test_clamp_timeout_bounds():
    assert clamp_timeout(0) == 1
    assert clamp_timeout(-5) == 1
    assert clamp_timeout(30) == 30
    assert clamp_timeout(10_000) == EXEC_MAX_TIMEOUT
