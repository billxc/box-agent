"""MCP transport — HTTP server exposing BoxAgent tools to AI agents."""

from boxagent.transports.mcp.server import create_mcp_app

__all__ = ["create_mcp_app"]
