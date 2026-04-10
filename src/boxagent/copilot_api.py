"""Copilot API proxy lifecycle management."""

import asyncio
import logging
import shutil
import socket
import sys
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT = 15  # seconds to wait for proxy to become ready
_HEALTH_INTERVAL = 0.5
_TOKEN_POLL_INTERVAL = 30  # seconds between token file checks


def find_token_path() -> Path | None:
    """Find the GitHub Copilot token file."""
    # xc-copilot-api uses ~/.local/share/copilot-api/ on all platforms
    base = Path.home() / ".local" / "share" / "copilot-api"
    token_path = base / "github_token"
    return token_path if token_path.is_file() else None


def get_free_port() -> int:
    """Get a free port from the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def check_health(port: int) -> bool:
    """Check if copilot-api is responding on the given port."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/v1/models")
            return resp.status_code == 200
    except Exception:
        return False


async def start_copilot_api(port: int = 0) -> asyncio.subprocess.Process | None:
    """Start copilot-api on a given port (or random if 0).

    Returns the process on success, None on failure.
    """
    token = find_token_path()
    if not token:
        logger.warning(
            "copilot_api enabled but no github_token found. "
            "Run 'xc-copilot-api auth' to authenticate. "
            "Will auto-start once token is available."
        )
        return None

    xca = shutil.which("xc-copilot-api") or shutil.which("xc-copilot-api.exe")
    if not xca:
        logger.warning("copilot_api enabled but xc-copilot-api not found in PATH")
        return None

    if not port:
        port = get_free_port()
    logger.info("Starting copilot-api on port %d...", port)

    proc = await asyncio.create_subprocess_exec(
        xca, "start", "--port", str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # create new process group for clean tree kill
    )

    # Wait for health
    for _ in range(int(_HEALTH_TIMEOUT / _HEALTH_INTERVAL)):
        if proc.returncode is not None:
            logger.error("copilot-api exited early with code %d", proc.returncode)
            return None
        if await check_health(port):
            logger.info("copilot-api ready on port %d (pid=%d)", port, proc.pid)
            return proc
        await asyncio.sleep(_HEALTH_INTERVAL)

    logger.error("copilot-api failed to become ready within %ds", _HEALTH_TIMEOUT)
    try:
        proc.kill()
        await proc.wait()
    except Exception:
        pass
    return None


async def stop_copilot_api(proc: asyncio.subprocess.Process) -> None:
    """Stop a copilot-api process and its entire process tree.

    npx spawns cmd → node subprocesses that don't receive SIGTERM from the
    parent. We must kill the entire process tree to avoid zombie processes.
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    logger.info("Stopping copilot-api (pid=%d)...", pid)
    try:
        if sys.platform == "win32":
            # Windows: taskkill /T kills the process tree
            import subprocess as sp
            sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=10)
        else:
            # Unix: kill the process group
            import os
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if sys.platform == "win32":
                import subprocess as sp
                sp.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
            else:
                import os
                import signal
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            await proc.wait()
    except Exception as e:
        logger.warning("Failed to stop copilot-api: %s", e)


def copilot_env_for_backend(ai_backend: str, port: int) -> dict[str, str]:
    """Build environment variables to inject for a given backend.

    Returns a dict of env vars that point the backend at the local proxy.
    Claude CLI uses env vars; Codex uses -c args (see copilot_args_for_codex).
    """
    if ai_backend == "claude-cli":
        return {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
        }
    # Codex backends don't use env vars for base_url; use copilot_args_for_codex() instead
    return {}


def copilot_args_for_codex(port: int) -> list[str]:
    """Build complete -c args for Codex/Codex ACP copilot-api provider.

    Use a single table assignment because the provider key contains a
    hyphen (``copilot-api``). Dotted overrides like
    ``model_providers.copilot-api.name=...`` can be parsed ambiguously by
    Codex/TOML as subtraction tokens instead of a quoted key.
    """
    return [
        "-c", 'model_provider="copilot-api"',
        "-c", (
            'model_providers={ '
            f'"copilot-api" = {{ name = "copilot-api", base_url = "http://127.0.0.1:{port}/v1", wire_api = "responses" }} '
            '}'
        ),
    ]


def get_auth_message(code: str, url: str) -> str:
    """Format a user-facing auth message."""
    return (
        f"🔑 **GitHub Copilot 认证**\n\n"
        f"请打开 {url}\n"
        f"输入代码: `{code}`\n\n"
        f"完成后 copilot-api 会自动启动。"
    )
