"""Cluster topology — node descriptors, machine snapshots, peer broadcast.

Mounted as a mixin on Gateway. Provides:

- ``_local_machine_id`` / ``_local_role`` — this node's identity in the cluster
- ``_local_bot_descriptors`` — what this node's web layer exposes
- ``_collect_machines`` — full machine list (host only)
- ``_build_peer_descriptors`` — workgroup peers visible from this node
- ``_push_peers_snapshot_to_sats`` / ``_push_machines_snapshot_to_sats`` —
  fan out cluster state to connected guests
- ``_on_topology_change`` — single hook GuestRegistry calls on change
- ``_remote_session_for`` — host-side lookup of a guest session owning a bot
"""

import logging
import time

logger = logging.getLogger(__name__)


class TopologyMixin:
    def _local_machine_id(self) -> str:
        return self.config.machine_id or self.config.node_id or "local"

    def _local_role(self) -> str:
        """Current cluster role of this node — driven by HostElection."""
        rm = self._host_election
        if rm is None:
            return "single"
        state = getattr(rm, "state", "init")
        if state == "host":
            return "host"
        if state == "guest":
            return "guest"
        # init / standalone / unknown
        if self.config.cluster_tunnel:
            return "guest"  # default optimistic — manager hasn't promoted yet
        return "single"

    def _local_bot_descriptors(self) -> list[dict]:
        """List of {name, display_name, backend, model, kind} for everything web-enabled here."""
        out: list[dict] = []
        for name in self._web_channels:
            cfg = self.config.bots.get(name)
            workgroup = self.config.workgroups.get(name)
            if cfg is not None:
                out.append({
                    "name": name,
                    "display_name": cfg.display_name or name,
                    "backend": cfg.ai_backend,
                    "model": cfg.model,
                    "kind": "bot",
                })
            elif workgroup is not None:
                out.append({
                    "name": name,
                    "display_name": workgroup.display_name or name,
                    "backend": workgroup.ai_backend,
                    "model": workgroup.model,
                    "kind": "workgroup",
                })
        return out

    def _build_peer_descriptors(self, exclude: str = "") -> list[dict]:
        """List all workgroup admins reachable from this node, excluding *exclude*.

        Sources combined:
        - Local workgroups (from ``self._workgroup_mgr.routers``)
        - Remote workgroup-kind bots from connected guests
          (``self.guest_registry.list_bots()``)
        - Remote workgroup-kind bots from disconnected-but-known guests
          (``self.guest_registry.history``) — flagged ``online=False``
        - On a guest (no local registry): ``self.guest_client.remote_peers``
          pushed by host via ``peers_snapshot`` frames.

        Each entry: ``{name, machine, online, kind, description}``. Used by
        Router.get_peers → AgentEnv.peers → context block; admin AI uses the
        *name* field as the ``send_to_peer(target=…)`` argument.
        """
        out: list[dict] = []
        if self._workgroup_mgr is not None:
            for name in self._workgroup_mgr.routers:
                if name == exclude:
                    continue
                if name not in self.config.workgroups:
                    continue  # routers also holds specialists; only workgroup names here
                workgroup = self.config.workgroups[name]
                out.append({
                    "name": name,
                    "machine": "local",
                    "online": True,
                    "kind": "workgroup",
                    "description": workgroup.display_name or "",
                })
        if self.guest_registry is not None:
            for machine_id, bot in self.guest_registry.list_bots():
                if bot.kind != "workgroup" or bot.name == exclude:
                    continue
                out.append({
                    "name": bot.name,
                    "machine": machine_id,
                    "online": True,
                    "kind": "workgroup",
                    "description": bot.display_name or "",
                })
            seen = {(p["name"], p["machine"]) for p in out}
            for machine_id, info in (self.guest_registry.history or {}).items():
                for b in info.get("bots") or []:
                    if b.get("kind") != "workgroup":
                        continue
                    name = b.get("name") or ""
                    if name == exclude or (name, machine_id) in seen:
                        continue
                    out.append({
                        "name": name,
                        "machine": machine_id,
                        "online": False,
                        "kind": "workgroup",
                        "description": b.get("display_name") or "",
                    })
        elif self.guest_client is not None:
            # Guest mode: registry is None, but host pushes peers_snapshot
            # frames containing the cross-cluster workgroup peer list.
            for p in self.guest_client.remote_peers:
                if not isinstance(p, dict):
                    continue
                if p.get("name") == exclude:
                    continue
                out.append({
                    "name": p.get("name", ""),
                    "machine": p.get("machine", ""),
                    "online": bool(p.get("online", True)),
                    "kind": p.get("kind", "workgroup"),
                    "description": p.get("description", ""),
                })
        return out

    async def _push_peers_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        """Send each connected guest a `peers_snapshot` frame so its admin
        can see workgroups elsewhere in the cluster.

        Triggered by GuestRegistry on hello / bots_update / disconnect.
        Each guest receives a list filtered to exclude its own workgroup-kind
        bots (so it doesn't see itself as a peer).

        ``changed_machine_id`` is just informational (which guest's state moved);
        we always re-broadcast to everyone since one guest's change affects what
        the others can route to.
        """
        if self.guest_registry is None:
            return
        for machine_id, sess in list(self.guest_registry.sessions.items()):
            self_workgroup_names = {
                b.name for b in sess.bots if b.kind == "workgroup"
            }
            peers: list[dict] = []
            if self._workgroup_mgr is not None:
                for wg_name in self._workgroup_mgr.routers:
                    if wg_name in self_workgroup_names:
                        continue
                    if wg_name not in self.config.workgroups:
                        continue
                    workgroup = self.config.workgroups[wg_name]
                    peers.append({
                        "name": wg_name,
                        "machine": self.config.node_id or "host",
                        "online": True,
                        "kind": "workgroup",
                        "description": workgroup.display_name or "",
                    })
            for other_mid, other_bot in self.guest_registry.list_bots():
                if other_mid == machine_id:
                    continue  # don't tell a guest about itself
                if other_bot.kind != "workgroup":
                    continue
                if other_bot.name in self_workgroup_names:
                    continue
                peers.append({
                    "name": other_bot.name,
                    "machine": other_mid,
                    "online": True,
                    "kind": "workgroup",
                    "description": other_bot.display_name or "",
                })
            try:
                await sess.ws.send_json({"type": "peers_snapshot", "peers": peers})
            except Exception as e:
                logger.warning("peers_snapshot push to %s failed: %s", machine_id, e)

    async def _on_topology_change(self, changed_machine_id: str | None) -> None:
        """Single hook for GuestRegistry topology events. Fans out to both
        the workgroup peers snapshot and the cluster machines snapshot so each
        guest keeps an up-to-date view for its own webui."""
        await self._push_peers_snapshot_to_sats(changed_machine_id)
        await self._push_machines_snapshot_to_sats(changed_machine_id)

    def _collect_machines(self) -> list[dict]:
        """Build the same machine list `_handle_web_machines` returns. Pure
        helper so the snapshot pusher and the HTTP handler share one source.

        Host node only — sats don't run a registry. Returns host's local
        machine first, then every connected/known guest.
        """
        local_mid = self._local_machine_id()
        local_role = self._local_role()
        machines: list[dict] = [{
            "machine_id": local_mid,
            "online": True,
            "role": local_role,
            "self": True,
            "host_index": self.config.my_host_index,
            "bots": self._local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self.guest_registry is not None:
            for m in self.guest_registry.list_machines():
                m["role"] = "guest"
                m["self"] = False
                mid = m.get("machine_id") or ""
                m["host_index"] = self.config.host_priority.index(mid) if mid in self.config.host_priority else -1
                machines.append(m)
        return machines

    async def _push_machines_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        """Push the full cluster machine list to every connected guest so each
        guest's webui can render the same sidebar the host shows. Per-guest the
        snapshot is filtered to drop the receiving guest's own row (the guest
        already renders itself from local state with ``self: true``).
        """
        if self.guest_registry is None:
            return
        all_machines = self._collect_machines()
        for machine_id, sess in list(self.guest_registry.sessions.items()):
            filtered = [m for m in all_machines if m.get("machine_id") != machine_id]
            try:
                await sess.ws.send_json({"type": "machines_snapshot", "machines": filtered})
            except Exception as e:
                logger.warning("machines_snapshot push to %s failed: %s", machine_id, e)

    def _remote_session_for(self, machine_id: str, bot: str):
        """Return GuestSession owning `bot` on `machine_id`, or None."""
        if self.guest_registry is None:
            return None
        if self.guest_registry.get_bot(machine_id, bot) is None:
            return None
        return self.guest_registry.get(machine_id)
