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
    # topology + peer + web server now see workgroup_manager.
    # (workgroup_manager.routes ships with the manager; no separate setter.)
    gateway._topology.set_workgroup_manager(manager)
    gateway._peer.set_workgroup_manager(manager)
    gateway._web_server.set_workgroup_manager(manager)
    await manager.start_all_for_node(gateway.config.node_id)
    return manager
