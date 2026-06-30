"""Composition wiring for the workgroup module.

Keeps all ``WorkgroupManager`` assembly out of the gateway. The gateway
calls :func:`install_workgroup` behind ``if config.workgroups:`` — deleting
the workgroup module means deleting this file and that one guard, not
unpicking construction details from the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boxagent.workgroup import WorkgroupManager

if TYPE_CHECKING:
    from boxagent.gateway import Gateway
    from boxagent.sessions import Storage


async def install_workgroup(gateway: "Gateway", storage: "Storage") -> WorkgroupManager:
    """Construct the WorkgroupManager, wire it into topology / peer / web
    server, then start its bots for this node. Returns the live manager.
    """
    manager = WorkgroupManager(
        config=gateway.config.workgroups,
        config_dir=str(gateway.config_dir),
        node_id=gateway.config.node_id,
        local_dir=storage.local_dir,
        start_time=gateway._start_time,
        storage=storage,
        web_channels=gateway._bots.web_channels,
        _peer_provider=gateway._topology.build_peer_descriptors,
        gateway=gateway,
    )
    # peer + web server hold the manager directly; topology only needs the
    # names of locally-active workgroup admins, so it gets a provider callback
    # (keeps the cluster layer free of any WorkgroupManager import).
    gateway._topology.set_local_workgroup_provider(lambda: list(manager.routers.keys()))
    gateway._peer.set_workgroup_manager(manager)
    await manager.start_all_for_node(gateway.config.node_id)
    return manager
