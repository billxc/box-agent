"""Manage a dedicated devtunnel for the cluster's host node.

When this BoxAgent is the cluster host, it owns one tunnel exclusively for
the cluster (separate from any tunnels the user creates for personal
purposes). The tunnel name defaults to ``boxagent-cluster``.

Lifecycle on host startup:

1. ``devtunnel list -j`` → filter by bare name. Same name can exist in
   multiple regions (e.g. ``boxagent-cluster.asse`` vs
   ``boxagent-cluster.jpe1``); ``devtunnel show <name>`` only returns
   whatever the current region resolves to, hiding the duplicates and
   stranding guests on stale URLs. On >1 match we log a warning and
   pick the one with an active host connection — refusing to start
   would just leave the cluster down.
2. If zero matches, create with ``devtunnel create <name>``.
3. Resolve the full ``tunnelId`` (e.g. ``boxagent-cluster.asse``) and
   use it for every subsequent call so nothing is region-ambiguous.
4. Ensure port 9292 is registered (``devtunnel port create``).
5. Spawn ``devtunnel host <tunnel_id>`` as a child subprocess and keep
   it alive.
6. Poll ``devtunnel show <tunnel_id> -j`` to read the resolved
   ``portUri``; write it to ``{local_dir}/cluster-tunnel-url.txt`` so
   other tooling and guests can discover it.

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
    _tunnel_id: str = ""

    @property
    def url(self) -> str:
        return self._url

    async def start(self) -> str:
        """Ensure the tunnel exists, expose the port, and start hosting it.

        Returns the public ``portUri`` once resolved.
        """
        if not shutil.which("devtunnel"):
            raise RuntimeError("devtunnel CLI not found in PATH")

        tunnel_id = await self._resolve_tunnel_id()
        if tunnel_id is None:
            # Authenticated by default — only the same Microsoft tenant/user
            # can connect. Guests must hold a connect token issued by
            # `devtunnel token --scopes connect`.
            await self._run("create", self.name)
            tunnel_id = await self._resolve_tunnel_id()
            if tunnel_id is None:
                raise RuntimeError(f"failed to create tunnel '{self.name}'")
            logger.info("Cluster tunnel '%s' created (authenticated)", tunnel_id)
        self._tunnel_id = tunnel_id

        info = await self._show(tunnel_id)
        # Ensure port is registered
        ports = (info.get("ports") or []) if info else []
        if not any(int(p.get("portNumber") or 0) == self.port for p in ports):
            await self._run("port", "create", tunnel_id, "-p", str(self.port), "--protocol", "http")
            logger.info("Cluster tunnel '%s' port %d registered", tunnel_id, self.port)

        # Start host process (long-running, supervised — see _monitor_host).
        await self._launch_supervised()

        # Poll until URL is reachable in the metadata
        url = ""
        for _ in range(20):
            info = await self._show(tunnel_id)
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
                f"tunnel '{tunnel_id}' started but URL did not resolve"
            )

        self._url = url
        logger.info("Cluster tunnel '%s' ready: %s", tunnel_id, url)
        return url

    async def _spawn_host_proc(self) -> asyncio.subprocess.Process:
        """Spawn a fresh ``devtunnel host`` subprocess. Isolated for tests."""
        return await asyncio.create_subprocess_exec(
            "devtunnel", "host", self._tunnel_id or self.name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )

    async def _launch_supervised(self) -> None:
        """Spawn host process + start monitor task that respawns on death.

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
        """Watch the host process; on unintended exit, log + respawn with backoff.

        Loop exits when ``stop()`` flips ``_stopping`` true.
        """
        while not self._stopping:
            process = self._host_proc
            if process is None:
                return
            try:
                return_code = await process.wait()
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            logger.warning(
                "Cluster tunnel '%s' host process (pid=%d) exited rc=%s; respawning in %.1fs",
                self.name, process.pid, return_code, self._respawn_backoff_seconds,
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
        process = self._host_proc
        self._host_proc = None
        if process is not None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            else:
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
        # Cancel the monitor only after we've torn down the process, so the
        # monitor's `await process.wait()` returns (clean exit, no respawn).
        task = self._monitor_task
        self._monitor_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def is_alive(self) -> bool:
        """Whether the supervisor still owns a tunnel.

        True while ``_monitor_host`` is the authority — covers the brief
        respawn window where ``_host_proc`` is the dying child but a new
        one is about to be launched. Callers (e.g. HostElection) must
        use this instead of peeking at ``_host_proc`` directly, otherwise
        they race the supervisor and trigger spurious demotes.
        """
        if self._stopping:
            return False
        task = self._monitor_task
        return task is not None and not task.done()

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            "devtunnel", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _list_matching(self) -> list[dict]:
        """Return all tunnels whose bare name (region stripped) matches self.name.

        ``devtunnel show <name>`` is region-ambiguous — same name can exist
        in multiple regions, and show only returns the one the current
        region resolves to. Listing client-side gives us the full picture.
        """
        return_code, out, err = await self._run("list", "-j")
        if return_code != 0:
            logger.debug("devtunnel list rc=%d err=%s", return_code, err.strip())
            return []
        try:
            data = json.loads(out)
        except Exception:
            return []
        tunnels = data.get("tunnels") if isinstance(data, dict) else data
        if not isinstance(tunnels, list):
            return []
        matches: list[dict] = []
        for t in tunnels:
            tunnel_id = str(t.get("tunnelId") or "")
            bare = tunnel_id.split(".", 1)[0] if "." in tunnel_id else tunnel_id
            if bare == self.name:
                matches.append(t)
        return matches

    async def _resolve_tunnel_id(self) -> str | None:
        """Find our tunnel's full region-qualified ID, or None if absent.

        When more than one region has a tunnel with our bare name we log a
        loud warning and pick the first one (preferring any with an active
        host connection — that's most likely ours from a previous run).
        Starting anyway is intentional: refusing leaves the cluster down,
        and the operator can clean up the orphan whenever convenient.
        """
        matches = await self._list_matching()
        if not matches:
            return None
        if len(matches) > 1:
            ids = ", ".join(str(t.get("tunnelId") or "?") for t in matches)
            matches.sort(key=lambda t: int(t.get("hostConnections") or 0), reverse=True)
            chosen = str(matches[0].get("tunnelId") or "")
            logger.warning(
                "Multiple tunnels named '%s' across regions: %s. Using '%s'. "
                "Delete the unused one(s) with `devtunnel delete <tunnelId>`.",
                self.name, ids, chosen,
            )
            return chosen or None
        tunnel_id = str(matches[0].get("tunnelId") or "")
        return tunnel_id or None

    async def _show(self, tunnel_id: str) -> dict | None:
        return_code, out, err = await self._run("show", tunnel_id, "-j")
        if return_code != 0:
            if "Tunnel not found" in err or "Tunnel not found" in out:
                return None
            logger.debug("devtunnel show '%s' rc=%d err=%s", tunnel_id, return_code, err.strip())
            return None
        try:
            data = json.loads(out)
        except Exception:
            return None
        return data.get("tunnel") or {}
