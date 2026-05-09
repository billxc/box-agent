"""BoxAgent MCP HTTP server — exposes the registry to claude-cli / codex-cli.

Per-bot endpoints (path-based routing) carry tools filtered by capability
so each agent only sees what its env allows. Path → group mapping mirrors
the names SDK adapters use, so the agent sees the same tool namespaces
regardless of backend:

  /mcp/base      — group="base"     (schedule, sessions)
  /mcp/telegram  — group="telegram" (media — bots with telegram channel)
  /mcp/admin     — group="admin"    (workgroup admin tools)
  /mcp/peer      — group="peer"     (cross-admin messaging)

Tool definitions live in :mod:`boxagent.tools.builtin`. This file is just
the HTTP transport — adapters/mcp_http.py does the registry → FastMCP
conversion.

Per-request context (bot_name, chat_id) comes from HTTP headers
``X-BoxAgent-Bot-Name`` / ``X-BoxAgent-Chat-Id``, captured into ContextVars
by the middleware below and read by the adapter at handler-call time.
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route

# Importing builtin triggers @boxagent_tool side-effect registration.
import boxagent.tools.builtin  # noqa: F401
from boxagent.tools import tools_for
from boxagent.tools.adapters.mcp_http import register_into

if TYPE_CHECKING:
    from boxagent.gateway import Gateway

logger = logging.getLogger(__name__)


# ── Per-request context (set by middleware from HTTP headers) ──

_ctx_bot_name: ContextVar[str] = ContextVar("bot_name", default="")
_ctx_chat_id: ContextVar[str] = ContextVar("chat_id", default="")


class _ContextMiddleware(BaseHTTPMiddleware):
    """Extract X-BoxAgent-* headers and store in ContextVars."""

    async def dispatch(self, request, call_next):
        bot = request.headers.get("x-boxagent-bot-name", "")
        chat = request.headers.get("x-boxagent-chat-id", "")
        t1 = _ctx_bot_name.set(bot)
        t2 = _ctx_chat_id.set(chat)
        try:
            return await call_next(request)
        finally:
            _ctx_bot_name.reset(t1)
            _ctx_chat_id.reset(t2)


def _make_mcp(name: str, path: str) -> FastMCP:
    return FastMCP(name, stateless_http=True, streamable_http_path=path)


# Path → (server name, registry group) mapping.
# Server names match the SDK adapter naming (boxagent / boxagent-telegram /
# boxagent-admin / boxagent-peer) so the LLM sees consistent tool prefixes.
_ENDPOINTS = [
    ("/mcp/base",     "boxagent",          "base"),
    ("/mcp/telegram", "boxagent-telegram", "telegram"),
    ("/mcp/admin",    "boxagent-admin",    "admin"),
    ("/mcp/peer",     "boxagent-peer",     "peer"),
]


def create_mcp_app(
    *,
    config_dir: str,
    local_dir: str,
    node_id: str,
    gateway: Gateway,
) -> Starlette:
    """Build a Starlette ASGI app with one MCP endpoint per tool group."""
    mcps: list[FastMCP] = []

    for path, server_name, group in _ENDPOINTS:
        mcp = _make_mcp(server_name, path)
        register_into(
            mcp,
            tools_for(group=group),
            bot_name_var=_ctx_bot_name,
            chat_id_var=_ctx_chat_id,
            gateway=gateway,
            config_dir=config_dir,
            local_dir=local_dir,
            node_id=node_id,
        )
        mcps.append(mcp)

    routes: list[Route] = []
    for m in mcps:
        sub_app = m.streamable_http_app()
        routes.extend(sub_app.routes)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for m in mcps:
                await stack.enter_async_context(m.session_manager.run())
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(_ContextMiddleware)
    return app
