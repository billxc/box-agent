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

Ownership: this object owns ``tunnel`` / ``registry`` / ``client`` for the
lifetime of the elected role. Callers (Gateway) read them via the public
attributes; they don't write back. Topology-change and bot-descriptor
callbacks are injected at construction so this module never reaches into
``gateway._xxx`` private state.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TYPE_CHECKING

import aiohttp

from . import devtunnel
from .guest_client import GuestClient
from .registry import GuestRegistry
from .tunnel import ClusterTunnel

if TYPE_CHECKING:
    from ..config import AppConfig

logger = logging.getLogger(__name__)


TopologyChangeCb = Callable[[str | None], Awaitable[None]]
BotProviderCb = Callable[[], list[dict]]


@dataclass
class HostElection:
    """Decides this node's host/guest role and keeps it in sync with reality."""

    config: "AppConfig"
    on_topology_change: TopologyChangeCb | None = None
    bot_provider: BotProviderCb | None = None
    probe_interval: float = 10.0

    state: str = "init"  # "init" | "host" | "guest" | "standalone"
    current_upstream: str = ""

    # Owned cluster components — populated by transitions, read by Gateway.
    tunnel: ClusterTunnel | None = field(default=None, repr=False)
    registry: GuestRegistry | None = field(default=None, repr=False)
    client: GuestClient | None = field(default=None, repr=False)

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
            logger.warning("host election: initial tick failed: %s", e)
        self._task = asyncio.create_task(self._run(), name="cluster-host-election")

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
                logger.warning("host election tick failed: %s", e)

    async def _fire_topology_change(self, changed: str | None) -> None:
        if self.on_topology_change is None:
            return
        try:
            await self.on_topology_change(changed)
        except Exception:
            pass

    # ── core decision logic ──

    async def _tick(self) -> None:
        if not self.config.cluster_tunnel:
            self.state = "standalone"
            return

        priority = self.config.host_priority
        my_idx = self.config.my_host_index
        me = self.config.machine_id

        # Always probe ground truth first — never trust our own self-belief.
        # Catches the case where our `devtunnel host` subprocess died after
        # promote (guest tunnel claimed by a peer, devtunnel quirks, etc.) so
        # we'd otherwise stay in a "host" delusion forever.
        upstream = await self._probe_active_host()

        if self.state == "host":
            # Sanity: is the tunnel actually serving *us*?
            tunnel = self.tunnel
            tunnel_dead = tunnel is None or not tunnel.is_alive()
            stolen = upstream and upstream != me
            if tunnel_dead or stolen:
                logger.warning(
                    "host election: lost host status (tunnel_dead=%s, "
                    "probe_says='%s', expected='%s') — demoting",
                    tunnel_dead, upstream, me,
                )
                await self._become_guest(upstream or "")
                # Re-tick immediately so we either promote again (if no other
                # host appeared) or settle into the new upstream.
                await self._tick()
                return

            # If the probe failed (no upstream visible) but our subprocess is
            # alive, accept that — could be a transient devtunnel show hiccup.
            # Then check whether a higher-priority candidate has joined as a
            # guest and we should yield.
            registry = self.registry
            if registry is not None and my_idx > 0:
                for sess_mid in list(registry.sessions.keys()):
                    if sess_mid in priority and priority.index(sess_mid) < my_idx:
                        logger.info(
                            "host election: demoting; higher-priority candidate '%s' is here",
                            sess_mid,
                        )
                        await self._become_guest(sess_mid)
                        return
            return

        # Not host yet — settle into guest or promote.
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
            url = await devtunnel.resolve_url(tunnel, port=self.config.web_port or 9292)
        except Exception as e:
            logger.debug("host election: tunnel resolve failed: %s", e)
            return ""
        try:
            token = await devtunnel.connect_token(tunnel)
        except Exception as e:
            logger.debug("host election: devtunnel token mint failed: %s", e)
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
            logger.debug("host election: probe /api/version failed: %s", e)
            return ""

    # ── transitions ──

    async def _try_promote(self) -> None:
        async with self._transition_lock:
            if self.state == "host":
                return
            logger.info("host election: attempting promote to active host")
            await self._teardown_guest()

            tunnel = ClusterTunnel(
                name=self.config.cluster_tunnel,
                port=self.config.web_port or 9292,
            )
            try:
                url = await tunnel.start()
            except Exception as e:
                logger.warning("host election: promote failed (tunnel host busy?): %s", e)
                # Fall back to guest mode — someone else owns the tunnel.
                await self._ensure_guest_locked("")
                return
            self.tunnel = tunnel

            self.registry = GuestRegistry(
                expected_token=self.config.guest_token or self.config.host_token,
                on_topology_change=self.on_topology_change,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token or "",
            )

            self.state = "host"
            self.current_upstream = ""
            logger.info("host election: promoted to active host (tunnel %s)", url)
            # Refresh sidebar for whoever's currently watching.
            await self._fire_topology_change(None)

    async def _ensure_guest(self, upstream: str) -> None:
        async with self._transition_lock:
            await self._ensure_guest_locked(upstream)

    async def _ensure_guest_locked(self, upstream: str) -> None:
        if self.state == "host":
            await self._teardown_host()

        if self.client is None:
            mid = self.config.machine_id or self.config.node_id or "guest"
            self.client = GuestClient(
                host_url="",
                host_token=self.config.host_token,
                machine_id=mid,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token or "",
                tunnel_name=self.config.cluster_tunnel,
                bot_provider=self.bot_provider or (lambda: []),
            )
            self.client.start()
            logger.info(
                "host election: guest mode — dialing tunnel '%s' (upstream=%s)",
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
        if self.registry is not None:
            try:
                await self.registry.close_all_sessions()
            except Exception as e:
                logger.warning("host election: close registry sessions failed: %s", e)
            try:
                await self.registry.aclose()
            except Exception:
                pass
            self.registry = None

        if self.tunnel is not None:
            try:
                await self.tunnel.stop()
            except Exception as e:
                logger.warning("host election: stop cluster tunnel failed: %s", e)
            self.tunnel = None

    async def _teardown_guest(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception as e:
                logger.warning("host election: stop guest client failed: %s", e)
            self.client = None

    async def _teardown_all(self) -> None:
        await self._teardown_host()
        await self._teardown_guest()
