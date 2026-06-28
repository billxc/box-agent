"""Peer messaging — local + cluster RPC dispatch.

Owned by the workgroup module: peer messaging only exists between
workgroup admins, so the gateway constructs this **only when
``config.workgroups`` is non-empty** (``self._peer`` is ``None``
otherwise). Deleting the workgroup module removes this file and the
peer routes vanish with it.

Two-phase DI:

- Phase 1 (constructor): topology (for guest_registry/guest_client lookups)
  and main_chat_id_provider (callable that mints/loads the admin's main
  chat_id for envelope dispatch).
- Phase 2 (setter): ``set_workgroup_manager`` after WorkgroupManager built.

Public surface:
- ``send_peer`` — cluster-aware peer message dispatch (used by MCP tool
  and by the /api/peer/send HTTP route).
- ``handle_peer_send`` / ``handle_wg_peer_recv`` — aiohttp handlers
  registered by ClusterHttpRoutes (only when peer is present) and the
  internal API HTTP server.
"""

import logging
from typing import TYPE_CHECKING, Callable

from aiohttp import web

if TYPE_CHECKING:
    from boxagent.cluster.topology_service import TopologyService
    from boxagent.workgroup.manager import WorkgroupManager

logger = logging.getLogger(__name__)


class PeerService:
    def __init__(
        self,
        *,
        topology: "TopologyService",
        main_chat_id_provider: Callable[[str], str],
    ) -> None:
        self.topology = topology
        self._main_chat_id_provider = main_chat_id_provider
        # Phase 2 dep
        self.workgroup_manager: "WorkgroupManager | None" = None

    def set_workgroup_manager(self, workgroup_manager: "WorkgroupManager") -> None:
        self.workgroup_manager = workgroup_manager

    async def send_peer(
        self, target: str, sender: str, message: str,
    ) -> dict:
        """Cluster-aware cross-admin peer message dispatch.

        Resolves target locally first, falls back to guest RPC. Used by
        both the HTTP route /api/peer/send and the MCP send_to_peer tool.

        Returns ``{ok: bool, via: "local"|"rpc"|"none", machine?: str, error?: str}``.
        """
        if (
            self.workgroup_manager is not None
            and target in self.workgroup_manager.routers
        ):
            await self._dispatch_local_peer(target, sender, message)
            return {"ok": True, "via": "local"}

        # Host mode: registry visible, look for the target on a guest.
        # Guest mode: registry is None, but guest_client lets us forward
        # to the host so it can resolve. HostElection guarantees these
        # are mutually exclusive — never both set, so an elif is correct.
        guest_registry = self.topology.guest_registry
        guest_client = self.topology.guest_client
        if guest_registry is not None:
            for machine_id, bot in guest_registry.list_bots():
                if bot.name != target or bot.kind != "workgroup":
                    continue
                session = guest_registry.get(machine_id)
                if session is None:
                    continue
                try:
                    rpc_result = await session.call(
                        "POST", "/api/wg/peer/recv",
                        body={"target_workgroup": target, "sender": sender, "body": message},
                    )
                except Exception as e:
                    logger.error("Peer RPC to %s failed: %s", machine_id, e)
                    return {"ok": False, "via": "rpc", "error": f"rpc failed: {e}"}
                # Don't trust GuestSession.call's transport-level success —
                # the guest-side handler may have returned 404/500. Surface
                # non-2xx so callers don't think the message was delivered.
                status = int(rpc_result.get("status") or 0)
                if 200 <= status < 300:
                    return {"ok": True, "via": "rpc", "machine": machine_id}
                body = rpc_result.get("body") or {}
                err = body.get("error") if isinstance(body, dict) else None
                return {
                    "ok": False, "via": "rpc", "machine": machine_id,
                    "error": f"guest returned status={status}: {err or body}",
                }
        elif guest_client is not None:
            try:
                result = await guest_client.fetch_host_json(
                    "/api/peer/send", method="POST",
                    body={"target": target, "from": sender, "message": message},
                )
            except Exception as e:
                logger.error("Peer fwd to host failed: %s", e)
                return {"ok": False, "via": "host-fwd", "error": f"host fwd failed: {e}"}
            if result.get("ok"):
                return {"ok": True, "via": "host-fwd", "machine": result.get("machine", "")}
            return {
                "ok": False, "via": "host-fwd",
                "error": result.get("error") or "host returned not-ok",
            }
        return {
            "ok": False, "via": "none",
            "error": f"no workgroup '{target}' found locally or in cluster",
        }

    async def handle_peer_send(self, request: web.Request) -> web.Response:
        """POST /api/peer/send — thin wrapper around send_peer."""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = payload.get("target", "")
        message = payload.get("message", "")
        from_bot = payload.get("from", "")
        if not target or not message or not from_bot:
            return web.json_response(
                {"ok": False, "error": "missing 'target', 'message', or 'from'"},
                status=400,
            )

        result = await self.send_peer(target, from_bot, message)
        if not result.get("ok"):
            via = result.get("via")
            status = 502 if via == "rpc" else 404
            return web.json_response(result, status=status)
        return web.json_response(result)

    async def _dispatch_local_peer(self, target: str, sender: str, body: str) -> None:
        """Inject a peer message into the local workgroup admin's router."""
        if self.workgroup_manager is None:
            raise RuntimeError(f"PeerService.workgroup_manager not set; cannot dispatch to {target!r}")
        admin_router = self.workgroup_manager.routers[target]
        envelope = (
            f"[Peer message from {sender}]\n"
            f"{body}\n\n"
            f"---\n"
            f'Reply with: send_to_peer("{sender}", "your reply")'
        )
        from boxagent.transports.base import IncomingMessage
        msg = IncomingMessage(
            channel="internal",
            chat_id=self._main_chat_id_provider(target),
            user_id=sender,
            text=envelope,
            trusted=True,
        )
        await admin_router.handle_message(msg)

    async def handle_wg_peer_recv(self, request: web.Request) -> web.Response:
        """POST /api/wg/peer/recv — receive a peer message from another node.

        Body: {target_workgroup, sender, body} where body is RAW (no envelope).
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        target = payload.get("target_workgroup", "")
        sender = payload.get("sender", "")
        body = payload.get("body", "")
        if not target or not sender:
            return web.json_response(
                {"ok": False, "error": "missing 'target_workgroup' or 'sender'"},
                status=400,
            )
        if (
            self.workgroup_manager is None
            or target not in self.workgroup_manager.routers
        ):
            return web.json_response(
                {"ok": False, "error": f"workgroup '{target}' not on this node"},
                status=404,
            )
        await self._dispatch_local_peer(target, sender, body)
        return web.json_response({"ok": True})
