"""Unit tests for transports/mcp/server — registry-driven HTTP MCP app."""

from unittest.mock import MagicMock

import boxagent.tools.builtin  # noqa: F401  trigger registry population
from boxagent.tools import tools_for


class TestMCPHttpApp:
    """Verify the MCP HTTP app is built from the unified tool registry."""

    def _gateway_stub(self):
        gateway = MagicMock()
        gateway.config = MagicMock()
        gateway.config.bots = {}
        gateway.config.telegram_bots = {}
        return gateway

    def test_create_mcp_app_returns_starlette(self):
        from starlette.applications import Starlette
        from boxagent.transports.mcp.server import create_mcp_app

        app = create_mcp_app(
            config_dir="/tmp/test-config",
            local_dir="/tmp/test-local",
            node_id="test-node",
            gateway=self._gateway_stub(),
        )
        assert isinstance(app, Starlette)

    def test_app_has_one_endpoint_per_group(self):
        """2 endpoints (base/telegram) → 2+ routes (each MCP server
        contributes its own streamable_http_path)."""
        from boxagent.transports.mcp.server import create_mcp_app, _ENDPOINTS

        app = create_mcp_app(
            config_dir="/tmp/config", local_dir="/tmp/loc",
            node_id="n", gateway=self._gateway_stub(),
        )
        assert len(_ENDPOINTS) == 2
        # Every endpoint path appears at least once in the route table.
        route_paths = {getattr(r, "path", "") for r in app.routes}
        for path, _server, _group in _ENDPOINTS:
            assert any(path in p for p in route_paths), f"missing route for {path}"

    def test_context_middleware_registered(self):
        from boxagent.transports.mcp.server import create_mcp_app, _ContextMiddleware

        app = create_mcp_app(
            config_dir="/tmp/config", local_dir="/tmp/loc",
            node_id="n", gateway=self._gateway_stub(),
        )
        # Starlette stores middleware in user_middleware; check ours is in there.
        names = [type(mw.cls).__name__ if hasattr(mw, "cls") else str(mw)
                 for mw in app.user_middleware]
        names_str = " ".join(names)
        assert "_ContextMiddleware" in names_str or any(
            "_ContextMiddleware" in str(mw) for mw in app.user_middleware
        )

    def test_endpoints_match_registry_groups(self):
        """Every group declared in _ENDPOINTS has at least one tool, OR the
        tool list is intentionally empty for that group."""
        from boxagent.transports.mcp.server import _ENDPOINTS

        for path, server_name, group in _ENDPOINTS:
            tools = tools_for(group=group)
            # We don't require non-empty — just sanity-check the group key.
            assert group in {"base", "telegram"}, (
                f"unknown group {group} in _ENDPOINTS"
            )
            for t in tools:
                assert t.group == group


class TestContextVarPropagation:
    """Header → ContextVar middleware contract."""

    def test_middleware_reads_headers_into_contextvars(self):
        import asyncio
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.responses import PlainTextResponse
        from starlette.testclient import TestClient
        from boxagent.transports.mcp.server import (
            _ContextMiddleware, _ctx_bot_name, _ctx_chat_id,
        )

        seen: dict[str, str] = {}

        async def handler(request):
            seen["bot"] = _ctx_bot_name.get()
            seen["chat"] = _ctx_chat_id.get()
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/probe", handler)])
        app.add_middleware(_ContextMiddleware)

        client = TestClient(app)
        client.get(
            "/probe",
            headers={"X-BoxAgent-Bot-Name": "alpha", "X-BoxAgent-Chat-Id": "7"},
        )
        assert seen == {"bot": "alpha", "chat": "7"}
