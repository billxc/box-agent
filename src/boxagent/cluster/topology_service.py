"""Cluster topology — node descriptors, machine snapshots, peer broadcast.

Composition class. Held by Gateway as ``self._topology``. Two-phase DI:

- Phase 1 (constructor): config + shared web_channels dict (read for the
  local bot list).
- Phase 2 (setters): ``set_host_election`` / ``set_workgroup_mgr`` after
  those siblings exist.

Public surface (no leading underscore):
- ``local_machine_id`` / ``local_role`` / ``local_bot_descriptors``
- ``collect_machines`` / ``build_peer_descriptors``
- ``push_peers_snapshot_to_sats`` / ``push_machines_snapshot_to_sats``
- ``on_topology_change`` (single hook GuestRegistry calls on change)
- ``remote_session_for`` (host-side lookup of a guest session owning a bot)
"""

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxagent.cluster.host_election import HostElection
    from boxagent.config import AppConfig
    from boxagent.workgroup import WorkgroupManager

logger = logging.getLogger(__name__)


class TopologyService:
    def __init__(
        self,
        *,
        config: "AppConfig",
        web_channels: dict,
    ) -> None:
        self.config = config
        self.web_channels = web_channels
        # Phase 2 deps
        self.host_election: "HostElection | None" = None
        self.workgroup_mgr: "WorkgroupManager | None" = None

    def set_host_election(self, host_election: "HostElection") -> None:
        self.host_election = host_election

    def set_workgroup_mgr(self, workgroup_mgr: "WorkgroupManager") -> None:
        self.workgroup_mgr = workgroup_mgr

    # ── HostElection-owned views (re-exposed read-only) ──

    @property
    def guest_registry(self):
        he = self.host_election
        return he.registry if he is not None else None

    @property
    def guest_client(self):
        he = self.host_election
        return he.client if he is not None else None

    # ── Local identity ──

    def local_machine_id(self) -> str:
        return self.config.machine_id or self.config.node_id or "local"

    def local_role(self) -> str:
        rm = self.host_election
        if rm is None:
            return "single"
        state = getattr(rm, "state", "init")
        if state == "host":
            return "host"
        if state == "guest":
            return "guest"
        if self.config.cluster_tunnel:
            return "guest"
        return "single"

    def local_bot_descriptors(self) -> list[dict]:
        out: list[dict] = []
        for name in self.web_channels:
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

    # ── Peer descriptors (workgroup admins reachable from this node) ──

    def build_peer_descriptors(self, exclude: str = "") -> list[dict]:
        out: list[dict] = []
        if self.workgroup_mgr is not None:
            for name in self.workgroup_mgr.routers:
                if name == exclude:
                    continue
                if name not in self.config.workgroups:
                    continue
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

    async def push_peers_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        if self.guest_registry is None:
            return
        for machine_id, session in list(self.guest_registry.sessions.items()):
            self_workgroup_names = {
                b.name for b in session.bots if b.kind == "workgroup"
            }
            peers: list[dict] = []
            if self.workgroup_mgr is not None:
                for wg_name in self.workgroup_mgr.routers:
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
                    continue
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
                await session.ws.send_json({"type": "peers_snapshot", "peers": peers})
            except Exception as e:
                logger.warning("peers_snapshot push to %s failed: %s", machine_id, e)

    async def on_topology_change(self, changed_machine_id: str | None) -> None:
        await self.push_peers_snapshot_to_sats(changed_machine_id)
        await self.push_machines_snapshot_to_sats(changed_machine_id)

    def collect_machines(self) -> list[dict]:
        local_machine_id = self.local_machine_id()
        local_role = self.local_role()
        machines: list[dict] = [{
            "machine_id": local_machine_id,
            "online": True,
            "role": local_role,
            "self": True,
            "host_index": self.config.my_host_index,
            "bots": self.local_bot_descriptors(),
            "last_seen": time.time(),
        }]
        if self.guest_registry is not None:
            for m in self.guest_registry.list_machines():
                m["role"] = "guest"
                m["self"] = False
                machine_id = m.get("machine_id") or ""
                m["host_index"] = self.config.host_priority.index(machine_id) if machine_id in self.config.host_priority else -1
                machines.append(m)
        return machines

    async def push_machines_snapshot_to_sats(self, changed_machine_id: str | None) -> None:
        if self.guest_registry is None:
            return
        all_machines = self.collect_machines()
        for machine_id, session in list(self.guest_registry.sessions.items()):
            filtered = [m for m in all_machines if m.get("machine_id") != machine_id]
            try:
                await session.ws.send_json({"type": "machines_snapshot", "machines": filtered})
            except Exception as e:
                logger.warning("machines_snapshot push to %s failed: %s", machine_id, e)

    def remote_session_for(self, machine_id: str, bot: str):
        if self.guest_registry is None:
            return None
        if self.guest_registry.get_bot(machine_id, bot) is None:
            return None
        return self.guest_registry.get(machine_id)
