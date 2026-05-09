"""Manage a dedicated devtunnel for the cluster's host node.

When this BoxAgent is the cluster host, it owns one tunnel exclusively for
the cluster (separate from any tunnels the user creates for personal
purposes). The tunnel name defaults to ``boxagent-cluster``.

Lifecycle on host startup:

1. ``devtunnel show <name> -j`` → if exists, reuse; else create with
   anonymous access (``devtunnel create <name> -a``).
2. Ensure port 9292 is registered (``devtunnel port create``).
3. Spawn ``devtunnel host <name>`` as a child subprocess and keep it alive.
4. Poll ``devtunnel show -j`` to read the resolved ``portUri``; write it to
   ``{local_dir}/cluster-tunnel-url.txt`` so other tooling and guests
   can discover it.

Anonymous access is fine for our threat model: the cluster.token in the WS
hello frame gates membership. devtunnels' browser interstitial only fires
on browser User-Agents — programmatic WebSocket clients are not affected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_TUNNEL_NAME = "boxagent-cluster"


@dataclass
class ClusterTunnel:
    name: str = DEFAULT_TUNNEL_NAME
    port: int = 9292

    _host_proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _monitor_task: asyncio.Task | None = field(default=None, repr=False)
    _stopping: bool = field(default=False, repr=False)
    _respawn_backoff_seconds: float = field(default=2.0, repr=False)
    _url: str = ""

    @property
    def url(self) -> str:
        return self._url

    async def start(self) -> str:
        """Ensure the tunnel exists, expose the port, and start hosting it.

        Returns the public ``portUri`` once resolved.
        """
        if not shutil.which("devtunnel"):
            raise RuntimeError("devtunnel CLI not found in PATH")

        info = await self._show()
        if info is None:
            # Authenticated by default — only the same Microsoft tenant/user
            # can connect. Guests must hold a connect token issued by
            # `devtunnel token --scopes connect`.
            await self._run("create", self.name)
            info = await self._show()
            if info is None:
                raise RuntimeError(f"failed to create tunnel '{self.name}'")
            logger.info("Cluster tunnel '%s' created (authenticated)", self.name)

        # Ensure port is registered
        ports = (info.get("ports") or []) if info else []
        if not any(int(p.get("portNumber") or 0) == self.port for p in ports):
            await self._run("port", "create", self.name, "-p", str(self.port), "--protocol", "http")
            logger.info("Cluster tunnel '%s' port %d registered", self.name, self.port)

        # Start host process (long-running, supervised — see _monitor_host).
        await self._launch_supervised()

        # Poll until URL is reachable in the metadata
        url = ""
        for _ in range(20):
            info = await self._show()
            if info:
                for p in (info.get("ports") or []):
                    if int(p.get("portNumber") or 0) == self.port:
                        url = str(p.get("portUri") or "").rstrip("/")
                        break
            if url:
                break
            await asyncio.sleep(0.5)
        if not url:
            raise RuntimeError(
                f"tunnel '{self.name}' started but URL did not resolve"
            )

        self._url = url
        logger.info("Cluster tunnel '%s' ready: %s", self.name, url)
        return url

    async def _spawn_host_proc(self) -> asyncio.subprocess.Process:
        """Spawn a fresh ``devtunnel host`` subprocess. Isolated for tests."""
        return await asyncio.create_subprocess_exec(
            "devtunnel", "host", self.name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )

    async def _launch_supervised(self) -> None:
        """Spawn host proc + start monitor task that respawns on death.

        Without supervision the devtunnel host process can die silently
        (macOS app nap, network drop, devtunnel internal error) and the
        cluster falls off the map until the next BoxAgent restart.
        """
        self._host_proc = await self._spawn_host_proc()
        logger.info(
            "Cluster tunnel '%s' host process started (pid=%d)",
            self.name, self._host_proc.pid,
        )
        self._monitor_task = asyncio.create_task(self._monitor_host())

    async def _monitor_host(self) -> None:
        """Watch the host proc; on unintended exit, log + respawn with backoff.

        Loop exits when ``stop()`` flips ``_stopping`` true.
        """
        while not self._stopping:
            proc = self._host_proc
            if proc is None:
                return
            try:
                rc = await proc.wait()
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            logger.warning(
                "Cluster tunnel '%s' host process (pid=%d) exited rc=%s; respawning in %.1fs",
                self.name, proc.pid, rc, self._respawn_backoff_seconds,
            )
            try:
                await asyncio.sleep(self._respawn_backoff_seconds)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                self._host_proc = await self._spawn_host_proc()
                logger.info(
                    "Cluster tunnel '%s' host process respawned (pid=%d)",
                    self.name, self._host_proc.pid,
                )
            except Exception as e:
                logger.error(
                    "Cluster tunnel '%s' respawn failed: %s — retrying after backoff",
                    self.name, e,
                )

    async def stop(self) -> None:
        self._stopping = True
        proc = self._host_proc
        self._host_proc = None
        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
        # Cancel the monitor only after we've torn down the proc, so the
        # monitor's `await proc.wait()` returns (clean exit, no respawn).
        task = self._monitor_task
        self._monitor_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "devtunnel", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _show(self) -> dict | None:
        rc, out, err = await self._run("show", self.name, "-j")
        if rc != 0:
            if "Tunnel not found" in err or "Tunnel not found" in out:
                return None
            logger.debug("devtunnel show '%s' rc=%d err=%s", self.name, rc, err.strip())
            return None
        try:
            data = json.loads(out)
        except Exception:
            return None
        return data.get("tunnel") or {}
