"""Runtime cluster role election & failover.

`cluster.host` is now an ordered fallback list (e.g. `[mbp, devbox-xl, macmini]`).
Whoever is highest-priority and reachable owns the cluster tunnel and serves
as the active host; everyone else runs as guest. Roles are decided at
runtime and re-evaluated periodically — primary going offline causes the
next-in-line to take over, primary coming back triggers the current host to
demote.

Promotion / demotion uses the same shared `cluster.tunnel_name`
(`boxagent-cluster`) — only one node hosts it at a time. We rely on
`devtunnel host` being mutually exclusive: only one process can host a tunnel
at any given moment.

State transitions:

  init ──probe──►  guest (someone else hosting)
       └───────►  host  (no one hosting; I'm a candidate)

  guest ──upstream gone, I'm next in line──► host
  host  ──higher-priority candidate appears as guest──► guest
  host  ──devtunnel host process exits unexpectedly──► guest (next probe re-elects)

The "higher-priority displaces lower" path needs no new protocol: when a
recovering primary boots, its first probe sees the lower-priority host
hosting the tunnel and joins it as a guest. The lower-priority host's next
tick spots the higher-priority sess in its registry and voluntarily demotes.
The recovering primary's next tick then finds no host and promotes itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp

from .guest_client import GuestClient, _devtunnel_connect_token, _devtunnel_resolve_url
from .registry import GuestRegistry
from .tunnel import ClusterTunnel

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..gateway import Gateway

logger = logging.getLogger(__name__)


@dataclass
class ClusterRoleManager:
    """Decides this node's host/guest role and keeps it in sync with reality."""

    config: "AppConfig"
    gateway: "Gateway"
    probe_interval: float = 10.0

    state: str = "init"  # "init" | "host" | "guest" | "standalone"
    current_upstream: str = ""

    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: bool = False
    _http: aiohttp.ClientSession | None = field(default=None, repr=False)
    _transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def is_host(self) -> bool:
        return self.state == "host"

    @property
    def is_guest(self) -> bool:
        return self.state == "guest"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        # Drive an immediate first tick before kicking off the periodic loop
        # so the role is settled before any web request lands.
        try:
            await self._tick()
        except Exception as e:
            logger.warning("role manager: initial tick failed: %s", e)
        self._task = asyncio.create_task(self._run(), name="cluster-role-manager")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._teardown_all()
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(self.probe_interval)
                if self._stop:
                    break
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("role manager tick failed: %s", e)

    # ── core decision logic ──

    async def _tick(self) -> None:
        if not self.config.cluster_tunnel:
            self.state = "standalone"
            return

        priority = self.config.host_priority
        my_idx = self.config.my_host_index
        me = self.config.machine_id

        if self.state == "host":
            # Stay host unless a higher-priority candidate has shown up as a guest.
            registry = self.gateway._guest_registry
            if registry is not None and my_idx > 0:
                for sess_mid in list(registry.sessions.keys()):
                    if sess_mid in priority and priority.index(sess_mid) < my_idx:
                        logger.info(
                            "role manager: demoting; higher-priority candidate '%s' is here",
                            sess_mid,
                        )
                        await self._become_guest(sess_mid)
                        return
            return

        # Not host yet — figure out who is.
        upstream = await self._probe_active_host()
        if upstream and upstream != me:
            await self._ensure_guest(upstream)
            return

        if my_idx >= 0:
            await self._try_promote()
        else:
            # Not a candidate, no host visible. Stay quiet; guest_client (if any)
            # will keep retrying once a host appears.
            await self._ensure_guest("")

    # ── probes ──

    async def _probe_active_host(self) -> str:
        """Resolve the cluster tunnel URL and ask whoever's hosting it for
        their machine_id via /api/version. Returns "" if no host is reachable.
        """
        tunnel = self.config.cluster_tunnel
        if not tunnel or not shutil.which("devtunnel"):
            return ""
        try:
            url = await _devtunnel_resolve_url(tunnel, port=self.config.web_port or 9292)
        except Exception as e:
            logger.debug("role manager: tunnel resolve failed: %s", e)
            return ""
        try:
            token = await _devtunnel_connect_token(tunnel)
        except Exception as e:
            logger.debug("role manager: devtunnel token mint failed: %s", e)
            return ""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        headers = {"X-Tunnel-Authorization": f"tunnel {token}"}
        if self.config.host_token:
            headers["Authorization"] = f"Bearer {self.config.host_token}"
        try:
            async with self._http.get(
                f"{url.rstrip('/')}/api/version",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json(content_type=None)
                return str(data.get("machine_id") or "")
        except Exception as e:
            logger.debug("role manager: probe /api/version failed: %s", e)
            return ""

    # ── transitions ──

    async def _try_promote(self) -> None:
        async with self._transition_lock:
            if self.state == "host":
                return
            logger.info("role manager: attempting promote to active host")
            await self._teardown_guest()

            tunnel = ClusterTunnel(
                name=self.config.cluster_tunnel,
                port=self.config.web_port or 9292,
            )
            try:
                url = await tunnel.start()
            except Exception as e:
                logger.warning("role manager: promote failed (tunnel host busy?): %s", e)
                # Fall back to guest mode — someone else owns the tunnel.
                await self._ensure_guest_locked("")
                return
            self.gateway._cluster_tunnel = tunnel

            registry = GuestRegistry(
                expected_token=self.config.guest_token or self.config.host_token,
                on_topology_change=self.gateway._on_topology_change,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token or "",
            )
            self.gateway._guest_registry = registry

            self.state = "host"
            self.current_upstream = ""
            logger.info("role manager: promoted to active host (tunnel %s)", url)
            # Refresh sidebar for whoever's currently watching.
            try:
                await self.gateway._on_topology_change(None)
            except Exception:
                pass

    async def _ensure_guest(self, upstream: str) -> None:
        async with self._transition_lock:
            await self._ensure_guest_locked(upstream)

    async def _ensure_guest_locked(self, upstream: str) -> None:
        if self.state == "host":
            await self._teardown_host()

        client = self.gateway._guest_client
        if client is None:
            mid = self.config.machine_id or self.config.node_id or "guest"
            client = GuestClient(
                host_url="",
                host_token=self.config.host_token,
                machine_id=mid,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token or "",
                tunnel_name=self.config.cluster_tunnel,
                bot_provider=self.gateway._local_bot_descriptors,
            )
            self.gateway._guest_client = client
            client.start()
            logger.info(
                "role manager: guest mode — dialing tunnel '%s' (upstream=%s)",
                self.config.cluster_tunnel, upstream or "?",
            )
        self.state = "guest"
        self.current_upstream = upstream

    async def _become_guest(self, upstream: str) -> None:
        async with self._transition_lock:
            await self._teardown_host()
            await self._ensure_guest_locked(upstream)

    # ── teardown helpers ──

    async def _teardown_host(self) -> None:
        registry = self.gateway._guest_registry
        if registry is not None:
            try:
                await registry.close_all_sessions()
            except Exception as e:
                logger.warning("role manager: close registry sessions failed: %s", e)
            try:
                await registry.aclose()
            except Exception:
                pass
            self.gateway._guest_registry = None

        tunnel = self.gateway._cluster_tunnel
        if tunnel is not None:
            try:
                await tunnel.stop()
            except Exception as e:
                logger.warning("role manager: stop cluster tunnel failed: %s", e)
            self.gateway._cluster_tunnel = None

    async def _teardown_guest(self) -> None:
        client = self.gateway._guest_client
        if client is not None:
            try:
                await client.stop()
            except Exception as e:
                logger.warning("role manager: stop guest client failed: %s", e)
            self.gateway._guest_client = None

    async def _teardown_all(self) -> None:
        await self._teardown_host()
        await self._teardown_guest()
