"""Peer messaging — local + cluster RPC dispatch."""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class PeerMixin:
    async def send_peer(
        self, target: str, sender: str, message: str,
    ) -> dict:
        """Cluster-aware cross-admin peer message dispatch.

        Resolves target locally first, falls back to guest RPC. Used by
        both the HTTP route /api/peer/send and the MCP send_to_peer tool.

        Returns ``{ok: bool, via: "local"|"rpc"|"none", machine?: str, error?: str}``.
        """
        if (
            self._workgroup_mgr is not None
            and target in self._workgroup_mgr.routers
        ):
            await self._dispatch_local_peer(target, sender, message)
            return {"ok": True, "via": "local"}
        if self.guest_registry is not None:
            for machine_id, bot in self.guest_registry.list_bots():
                if bot.name != target or bot.kind != "workgroup":
                    continue
                sess = self.guest_registry.get(machine_id)
                if sess is None:
                    continue
                try:
                    rpc_result = await sess.call(
                        "POST", "/api/wg/peer/recv",
                        body={"target_workgroup": target, "sender": sender, "body": message},
                    )
                except Exception as e:
                    logger.error("Peer RPC to %s failed: %s", machine_id, e)
                    return {"ok": False, "via": "rpc", "error": f"rpc failed: {e}"}
                # Don't trust GuestSession.call's transport-level success —
                # the guest-side handler may have returned 404/500 (e.g. wrong
                # port, unknown workgroup). Surface non-2xx as a real failure
                # so callers (and the admin AI) don't think the message was
                # delivered when it wasn't.
                status = int(rpc_result.get("status") or 0)
                if 200 <= status < 300:
                    return {"ok": True, "via": "rpc", "machine": machine_id}
                body = rpc_result.get("body") or {}
                err = body.get("error") if isinstance(body, dict) else None
                return {
                    "ok": False, "via": "rpc", "machine": machine_id,
                    "error": f"guest returned status={status}: {err or body}",
                }
        # Guest mode: not host, can't see registry — forward to host's
        # /api/peer/send and let host resolve. Without this, sats can only
        # peer-message workgroups they host themselves.
        if self.guest_client is not None:
            try:
                result = await self.guest_client.fetch_host_json(
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

    async def _handle_peer_send(self, request: web.Request) -> web.Response:
        """Handle POST /api/peer/send — thin wrapper around send_peer."""
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
        """Inject a peer message into the local workgroup admin's router.

        Wraps `body` in the workgroup peer envelope (admin always sees the
        same shape regardless of transport).

        Routed to the same chat_id heartbeat dispatches into
        (``heartbeat:<target>``) so the message lands in the admin's main
        session — not a separate ``peer:<sender>`` chat that would spawn a
        fresh, context-less session each time.
        """
        admin_router = self._workgroup_mgr.routers[target]
        envelope = (
            f"[Peer message from {sender}]\n"
            f"{body}\n\n"
            f"---\n"
            f'Reply with: send_to_peer("{sender}", "your reply")'
        )
        from boxagent.transports.base import IncomingMessage
        msg = IncomingMessage(
            channel="internal",
            chat_id=self._get_or_create_main_chat_id(target),
            user_id=sender,
            text=envelope,
            trusted=True,
        )
        await admin_router.handle_message(msg)

    async def _handle_wg_peer_recv(self, request: web.Request) -> web.Response:
        """Handle POST /api/wg/peer/recv — receive a peer message from another node.

        Body: {target_workgroup, sender, body} where body is RAW (no envelope).
        Caller (host's _handle_peer_send) routes here via cluster RPC; guest_client
        forwards it over WS to this local gateway.
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
            self._workgroup_mgr is None
            or target not in self._workgroup_mgr.routers
        ):
            return web.json_response(
                {"ok": False, "error": f"workgroup '{target}' not on this node"},
                status=404,
            )
        await self._dispatch_local_peer(target, sender, body)
        return web.json_response({"ok": True})
