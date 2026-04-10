"""Unit tests for MCP server — verify tool definitions and _send_media logic."""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestMCPServerTools:
    """Verify MCP server tool definitions load correctly."""

    def test_mcp_server_imports(self):
        """mcp_server.py can be imported without error."""
        # Set required env vars before import
        with patch.dict(os.environ, {
            "BOXAGENT_BOT_TOKEN": "test-token",
            "BOXAGENT_CHAT_ID": "12345",
        }):
            import importlib
            import boxagent.mcp_server as mod
            importlib.reload(mod)
            assert mod.BOT_TOKEN == "test-token"
            assert mod.CHAT_ID == "12345"

    def test_send_media_calls_httpx(self):
        """_send_media posts to Telegram API with correct params."""
        import tempfile
        from pathlib import Path

        with patch.dict(os.environ, {
            "BOXAGENT_BOT_TOKEN": "tok123",
            "BOXAGENT_CHAT_ID": "999",
        }):
            import importlib
            import boxagent.mcp_server as mod
            importlib.reload(mod)

            # Create a temp file to send
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
                f.write(b"hello")
                tmp_path = f.name

            try:
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()

                with patch("boxagent.mcp_server.httpx.post", return_value=mock_response) as mock_post:
                    result = mod._send_media("sendDocument", "document", tmp_path, "test caption")

                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args
                assert "sendDocument" in call_kwargs[0][0]
                assert call_kwargs[1]["data"]["chat_id"] == "999"
                assert call_kwargs[1]["data"]["caption"] == "test caption"
                assert "document" in call_kwargs[1]["files"]
                assert "Sent" in result
            finally:
                os.unlink(tmp_path)

    def test_send_media_no_caption(self):
        """_send_media works without caption."""
        import tempfile

        with patch.dict(os.environ, {
            "BOXAGENT_BOT_TOKEN": "tok",
            "BOXAGENT_CHAT_ID": "1",
        }):
            import importlib
            import boxagent.mcp_server as mod
            importlib.reload(mod)

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(b"fake png")
                tmp_path = f.name

            try:
                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()

                with patch("boxagent.mcp_server.httpx.post", return_value=mock_response) as mock_post:
                    mod._send_media("sendPhoto", "photo", tmp_path)

                call_kwargs = mock_post.call_args
                assert "caption" not in call_kwargs[1]["data"]
            finally:
                os.unlink(tmp_path)

    def test_all_tool_functions_exist(self):
        """All 5 MCP tools are defined as functions."""
        with patch.dict(os.environ, {
            "BOXAGENT_BOT_TOKEN": "t",
            "BOXAGENT_CHAT_ID": "1",
        }):
            import importlib
            import boxagent.mcp_server as mod
            importlib.reload(mod)

            for name in ["send_photo", "send_document", "send_video", "send_audio", "send_animation"]:
                assert hasattr(mod, name), f"Missing tool function: {name}"
