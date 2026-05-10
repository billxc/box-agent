"""Microsoft devtunnel CLI helpers used by both guest dial-in and host probes.

Both helpers shell out to the locally-installed ``devtunnel`` binary; the
calling machine must be logged in via ``devtunnel user login`` against the
same Microsoft account that owns the cluster tunnel.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil


async def resolve_url(tunnel_name: str, port: int = 9292) -> str:
    """Return the public ``portUri`` for ``tunnel_name`` on ``port``.

    Same Microsoft account only — that's our auth model.
    """
    if not shutil.which("devtunnel"):
        raise RuntimeError("devtunnel CLI not found on PATH")
    process = await asyncio.create_subprocess_exec(
        "devtunnel", "show", tunnel_name, "-j",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"devtunnel show '{tunnel_name}' failed: "
            + err.decode("utf-8", "replace").strip()
        )
    try:
        data = json.loads(out)
    except Exception as e:
        raise RuntimeError(f"devtunnel show: bad JSON: {e}")
    tunnel = data.get("tunnel") or {}
    for p in tunnel.get("ports") or []:
        if int(p.get("portNumber") or 0) == port:
            url = str(p.get("portUri") or "").rstrip("/")
            if url:
                return url
    raise RuntimeError(
        f"tunnel '{tunnel_name}' has no port {port} or hasn't been hosted yet"
    )


async def connect_token(tunnel_name: str) -> str:
    """Mint a connect-scope JWT via the locally-authenticated CLI.

    Without this token the guest cannot even reach the host's HTTP server —
    devtunnels gate membership at this layer.
    """
    if not shutil.which("devtunnel"):
        raise RuntimeError("devtunnel CLI not found on PATH")
    process = await asyncio.create_subprocess_exec(
        "devtunnel", "token", tunnel_name, "--scopes", "connect",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"devtunnel token failed: {err.decode('utf-8', 'replace').strip()}"
        )
    text = out.decode("utf-8", "replace")
    m = re.search(r"^Token:\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        raise RuntimeError("devtunnel token: no token in output")
    return m.group(1)


def tunnel_name_from_url(url: str) -> str:
    """Extract tunnel id from a portUri like https://abc-9292.jpe1.devtunnels.ms/."""
    m = re.match(r"https?://([^.-]+)(?:-\d+)?\.([^.]+)\.devtunnels\.ms/?", url)
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}"
