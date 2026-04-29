"""BoxAgent MCP server — consolidated HTTP (streamable-http) endpoint.

Runs as a long-lived server inside the Gateway process.  All tools from
the former stdio MCP servers (schedule, admin, peer, telegram) are merged
here.  Tools call Gateway methods directly — no HTTP round-trip.

Per-session context (bot_name, chat_id) is injected via HTTP headers:
  X-BoxAgent-Bot-Name, X-BoxAgent-Chat-Id
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.applications import Starlette

if TYPE_CHECKING:
    from boxagent.gateway import Gateway

logger = logging.getLogger(__name__)

# ── Per-request context (set by middleware from HTTP headers) ──

_ctx_bot_name: ContextVar[str] = ContextVar("bot_name", default="")
_ctx_chat_id: ContextVar[str] = ContextVar("chat_id", default="")

# ── Module-level refs set by create_mcp_app() ──

_gateway: Gateway | None = None
_config_dir: str = ""
_local_dir: str = ""
_node_id: str = ""


class _ContextMiddleware(BaseHTTPMiddleware):
    """Extract BoxAgent headers and store in ContextVars."""

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


# ── Helper ──

def _resolve_telegram_token(bot_name: str) -> str:
    """Look up Telegram bot token from Gateway config."""
    if not _gateway:
        return ""
    cfg = _gateway.config.bots.get(bot_name)
    if cfg and cfg.telegram_token:
        return cfg.telegram_token
    wg = _gateway.config.workgroups.get(bot_name)
    if wg:
        # Workgroup admin bots don't have direct telegram_token;
        # try the telegram_bots mapping.
        return _gateway.config.telegram_bots.get(bot_name, "")
    return ""


# ════════════════════════════════════════════════════════════════
#  Schedule tools
# ════════════════════════════════════════════════════════════════

def _register_schedule_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def schedule_list() -> str:
        """List all configured scheduled tasks with their cron, mode, and prompt."""
        if not _config_dir:
            return "Config dir not set."
        from boxagent.scheduler.cli import format_schedule_list
        return format_schedule_list(_config_dir, _node_id)

    @mcp.tool()
    def schedule_add(
        task_id: str,
        cron: str,
        prompt: str,
        mode: str = "isolate",
        bot: str = "",
        ai_backend: str = "",
        model: str = "",
    ) -> str:
        """Add a new scheduled task.

        Args:
            task_id: Unique task ID
            cron: Cron expression (5-field, e.g. "0 9 * * 1-5")
            prompt: Prompt to send when the schedule fires
            mode: Execution mode - "isolate" (standalone) or "append" (send to bot)
            bot: Bot name (required when mode=append)
            ai_backend: Backend for isolate mode (claude-cli, codex-cli, codex-acp)
            model: Model for isolate mode (e.g. sonnet, opus). Empty = default model
        """
        if not _config_dir:
            return "Config dir not set."
        from boxagent.scheduler.cli import add_schedule as _add
        return _add(
            config_dir=_config_dir,
            task_id=task_id,
            cron=cron,
            prompt=prompt,
            mode=mode,
            bot=bot,
            ai_backend=ai_backend,
            model=model,
        )

    @mcp.tool()
    def schedule_logs(task_id: str = "") -> str:
        """Show recent schedule execution logs.

        Args:
            task_id: Optional task ID to filter logs for a specific schedule
        """
        if not _local_dir:
            return "Local dir not set."
        from boxagent.scheduler.cli import format_schedule_logs
        return format_schedule_logs(_local_dir, task_id=task_id)

    @mcp.tool()
    def schedule_show(task_id: str) -> str:
        """Show detailed configuration for a specific scheduled task.

        Args:
            task_id: The schedule task ID to show
        """
        if not _config_dir:
            return "Config dir not set."
        from boxagent.scheduler.cli import format_schedule_show
        return format_schedule_show(_config_dir, _node_id, task_id)

    @mcp.tool()
    def schedule_run(task_id: str) -> str:
        """Trigger a scheduled task to run immediately (async).

        Args:
            task_id: The schedule task ID to run
        """
        if not _local_dir:
            return "Local dir not set."
        from boxagent.scheduler.cli import trigger_schedule_run
        return trigger_schedule_run(_local_dir, task_id)

    @mcp.tool()
    def schedule_run_detail(task_id: str, run_index: int = 1) -> str:
        """Show full details for a specific schedule run log entry.

        Args:
            task_id: The schedule task ID
            run_index: Which run to show (1 = most recent, 2 = second most recent, etc.)
        """
        if not _local_dir:
            return "Local dir not set."
        from boxagent.scheduler.cli import format_schedule_run_detail
        return format_schedule_run_detail(_local_dir, task_id, run_index)


# ════════════════════════════════════════════════════════════════
#  Session tools
# ════════════════════════════════════════════════════════════════

def _register_session_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def sessions_list(query: str = "", workspace: str = "") -> str:
        """Search and list sessions (Claude CLI + BoxAgent history + Codex).

        By default, only sessions matching *workspace* are shown.
        Use ``--all`` in the query to search across all projects.

        Query syntax (all tokens are optional, order-independent):
            --all           Show sessions from all projects (skip workspace filter)
            <keywords>      Text search on summary/prompt/project/path (multi-word AND)
            cwd:<substr>    Fuzzy match on session projectPath (bypasses workspace filter)
            grep:<substr>   Full-text search inside session JSONL content (applied last)
            <N>d            Only sessions modified in the last N days (e.g. 7d)
            backend:<name>  Filter by backend (e.g. claude-cli, codex-cli)
            bot:<name>      Filter by bot name
            p<N>            Page number (e.g. p2)
            <hex-prefix>    Lookup session by ID prefix (4+ hex chars)

        Args:
            query: Search query string (see syntax above)
            workspace: Project directory path to scope results (default: all projects)
        """
        from boxagent.sessions.cli import format_sessions_list
        from boxagent.sessions import Storage
        storage = Storage(_local_dir) if _local_dir else None
        return format_sessions_list(query=query, storage=storage, workspace=workspace)


# ════════════════════════════════════════════════════════════════
#  Workgroup admin tools
# ════════════════════════════════════════════════════════════════

def _register_admin_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def list_specialists() -> str:
        """List all specialist agents in your workgroup with their details.

        Returns each specialist's name, model, workspace, status, and whether
        it is a built-in or dynamically created specialist.
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        bot_name = _ctx_bot_name.get()
        result = _gateway._workgroup_mgr.list_specialists(bot_name)
        if not result.get("ok"):
            return f"Error: {result.get('error', 'unknown error')}"
        specialists = result.get("specialists", [])
        if not specialists:
            return "No specialists found in this workgroup."
        lines = []
        for sp in specialists:
            parts = [f"**{sp['name']}**"]
            if sp.get("display_name") and sp["display_name"] != sp["name"]:
                parts.append(f"({sp['display_name']})")
            parts.append(f"— model: {sp.get('model', 'default')}")
            if sp.get("workspace"):
                parts.append(f"| workspace: {sp['workspace']}")
            if sp.get("builtin"):
                parts.append("| built-in")
            else:
                parts.append("| dynamic")
            if sp.get("running_tasks"):
                parts.append(f"| running: {', '.join(sp['running_tasks'])}")
            lines.append(" ".join(parts))
        return f"Specialists ({len(specialists)}):\n" + "\n".join(lines)

    @mcp.tool()
    def get_specialist_status(agent_name: str) -> str:
        """Get a specialist's current status, recent tasks, and chat history.

        Args:
            agent_name: Name of the specialist to check
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        result = _gateway._workgroup_mgr.get_specialist_status(agent_name)
        if not result.get("ok"):
            return f"Error: {result.get('error', 'unknown error')}"
        lines = [f"**{agent_name}** — {'active' if result.get('active') else 'idle'}"]
        tasks = result.get("tasks", [])
        if tasks:
            lines.append(f"\nTasks ({len(tasks)}):")
            for t in tasks[-5:]:
                status = t.get("status", "?")
                tid = t.get("task_id", "?")
                preview = t.get("result_preview", "")
                error = t.get("error", "")
                line = f"  - {tid}: {status}"
                if preview:
                    line += f" — {preview[:100]}"
                if error:
                    line += f" — ERROR: {error[:100]}"
                lines.append(line)
        chat = result.get("recent_chat", [])
        if chat:
            lines.append(f"\nRecent chat ({len(chat)} lines):")
            for c in chat:
                lines.append(f"  {c}")
        return "\n".join(lines)

    @mcp.tool()
    async def send_to_agent(agent_name: str, message: str) -> str:
        """Dispatch a task to a specialist agent in your workgroup.

        The task is dispatched asynchronously — this tool returns immediately.
        The specialist processes the task in the background; results are visible
        in the specialist's Discord channel.

        Args:
            agent_name: Name of the specialist agent to delegate to
            message: The task description or question to send
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        bot_name = _ctx_bot_name.get()
        chat_id = _ctx_chat_id.get()
        try:
            result = await _gateway._workgroup_mgr.send_to_specialist(
                target=agent_name,
                text=message,
                from_bot=bot_name,
                reply_chat_id=chat_id,
            )
            if result.get("ok"):
                task_id = result.get("task_id", "")
                return f"Task dispatched to {agent_name} (task_id: {task_id}). Check the specialist's Discord channel for progress."
            return f"Error: {result.get('error', 'unknown error')}"
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    async def create_specialist(name: str, model: str = "") -> str:
        """Dynamically create a new specialist agent in your workgroup.

        Creates a Discord channel for the specialist and starts a new AI backend.
        The specialist gets its own isolated workspace directory automatically.

        Args:
            name: Unique name for the specialist (used as channel name too)
            model: AI model to use (default: inherit from workgroup)
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        bot_name = _ctx_bot_name.get()
        if not bot_name:
            return "Error: bot_name not set — cannot determine workgroup"
        try:
            result = await _gateway._workgroup_mgr.create_specialist(
                bot_name, name, model=model,
            )
            if result.get("ok"):
                ch_id = result.get("channel_id", 0)
                msg = f"Created specialist '{name}'"
                if ch_id:
                    msg += f" with Discord channel (ID: {ch_id})"
                return msg
            return f"Error: {result.get('error', 'unknown error')}"
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def reset_specialist(agent_name: str) -> str:
        """Reset a specialist's session so the next task starts with a clean context.

        Args:
            agent_name: Name of the specialist to reset
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        result = _gateway._workgroup_mgr.reset_specialist(agent_name)
        if result.get("ok"):
            return f"Specialist '{agent_name}' session reset. Next task will start fresh."
        return f"Error: {result.get('error', 'unknown error')}"

    @mcp.tool()
    async def delete_specialist(agent_name: str) -> str:
        """Delete a dynamically created specialist agent from your workgroup.

        Args:
            agent_name: Name of the specialist to delete
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        try:
            result = await _gateway._workgroup_mgr.delete_specialist(agent_name)
            if result.get("ok"):
                return f"Specialist '{agent_name}' deleted."
            return f"Error: {result.get('error', 'unknown error')}"
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    async def update_channel_topic(channel_id: str, topic: str) -> str:
        """Update the topic of a Discord channel.

        Args:
            channel_id: Discord channel ID to update
            topic: New topic text (max 1024 chars)
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        dc_channel = None
        for dc in _gateway._workgroup_mgr.discord_channels.values():
            dc_channel = dc
            break
        if dc_channel is None:
            return "Error: no Discord channel available"
        try:
            await dc_channel.update_channel_topic(int(channel_id), topic[:1024])
            return "Channel topic updated."
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    async def cancel_task(task_id: str) -> str:
        """Cancel a running specialist task.

        Args:
            task_id: The task ID to cancel (e.g. "dev-1-3")
        """
        if not _gateway or not _gateway._workgroup_mgr:
            return "Error: workgroup manager not available"
        try:
            result = await _gateway._workgroup_mgr.cancel_task(task_id)
            if result.get("ok"):
                return f"Task '{task_id}' cancelled."
            return f"Error: {result.get('error', 'unknown error')}"
        except Exception as e:
            return f"Error: {e}"


# ════════════════════════════════════════════════════════════════
#  Peer messaging tools
# ════════════════════════════════════════════════════════════════

def _register_peer_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def send_to_peer(target: str, message: str) -> str:
        """Send a message to another admin bot via the shared peer channel.

        The message is posted to the shared Discord peer channel. The target
        bot will receive and process it.

        Args:
            target: Name of the target bot (e.g. "win-bot", "mbp-bot")
            message: The message to send
        """
        if not _gateway:
            return "Error: gateway not available"
        bot_name = _ctx_bot_name.get()
        if not bot_name:
            return "Error: bot_name not set"

        # Find sender's peer channel
        peer_channel_id = 0
        dc_key = None
        bot_cfg = _gateway.config.bots.get(bot_name)
        if bot_cfg and bot_cfg.discord_peer_channel:
            peer_channel_id = bot_cfg.discord_peer_channel
            dc_key = _gateway._bot_discord_key.get(bot_name)
        else:
            wg_cfg = _gateway.config.workgroups.get(bot_name)
            if wg_cfg and wg_cfg.discord_peer_channel:
                peer_channel_id = wg_cfg.discord_peer_channel
                dc_key = wg_cfg.discord_bot_id

        if not peer_channel_id:
            return f"Error: bot '{bot_name}' has no peer channel configured"

        dc_channel = _gateway._discord_channels.get(dc_key) if dc_key else None
        if dc_channel is None:
            return f"Error: no Discord channel for bot '{bot_name}'"

        formatted = f"[To: {target}] [From: {bot_name}]\n{message}"
        try:
            await dc_channel.send_text(str(peer_channel_id), formatted)
            return f"Message sent to {target}."
        except Exception as e:
            return f"Error: {e}"


# ════════════════════════════════════════════════════════════════
#  Telegram media tools
# ════════════════════════════════════════════════════════════════

def _register_telegram_tools(mcp: FastMCP) -> None:

    def _send_media(method: str, field: str, file_path: str, caption: str = "") -> str:
        """Upload a file to Telegram via Bot API multipart POST."""
        bot_name = _ctx_bot_name.get()
        chat_id = _ctx_chat_id.get()
        if not chat_id:
            return "Error: chat_id not set"
        token = _resolve_telegram_token(bot_name)
        if not token:
            return f"Error: no Telegram token for bot '{bot_name}'"
        base_url = f"https://api.telegram.org/bot{token}"
        with open(file_path, "rb") as f:
            files = {field: f}
            data: dict[str, str] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = httpx.post(f"{base_url}/{method}", data=data, files=files, timeout=60)
            r.raise_for_status()
        return f"Sent {field} to chat {chat_id}"

    @mcp.tool()
    def send_photo(file_path: str, caption: str = "") -> str:
        """Send a photo/image to the user via Telegram.

        Args:
            file_path: Absolute path to the image file (jpg, png, etc.)
            caption: Optional caption text
        """
        return _send_media("sendPhoto", "photo", file_path, caption)

    @mcp.tool()
    def send_document(file_path: str, caption: str = "") -> str:
        """Send a file/document to the user via Telegram.

        Args:
            file_path: Absolute path to the file
            caption: Optional caption text
        """
        return _send_media("sendDocument", "document", file_path, caption)

    @mcp.tool()
    def send_video(file_path: str, caption: str = "") -> str:
        """Send a video to the user via Telegram.

        Args:
            file_path: Absolute path to the video file (mp4, etc.)
            caption: Optional caption text
        """
        return _send_media("sendVideo", "video", file_path, caption)

    @mcp.tool()
    def send_audio(file_path: str, caption: str = "") -> str:
        """Send an audio file to the user via Telegram.

        Args:
            file_path: Absolute path to the audio file (mp3, ogg, etc.)
            caption: Optional caption text
        """
        return _send_media("sendAudio", "audio", file_path, caption)

    @mcp.tool()
    def send_animation(file_path: str, caption: str = "") -> str:
        """Send a GIF animation to the user via Telegram.

        Args:
            file_path: Absolute path to the GIF file
            caption: Optional caption text
        """
        return _send_media("sendAnimation", "animation", file_path, caption)


# ════════════════════════════════════════════════════════════════
#  Factory
# ════════════════════════════════════════════════════════════════

def _make_mcp(name: str, path: str) -> FastMCP:
    """Create a FastMCP instance with a custom streamable_http_path."""
    return FastMCP(name, stateless_http=True, streamable_http_path=path)


def create_mcp_app(
    *,
    config_dir: str,
    local_dir: str,
    node_id: str,
    gateway: Gateway,
) -> Starlette:
    """Build a Starlette ASGI app with MCP tools on separate path-based endpoints.

    Endpoints:
      /mcp/base      — schedule + session tools (all bots)
      /mcp/admin     — workgroup admin tools
      /mcp/telegram  — Telegram media tools
      /mcp/peer      — peer messaging tools
    """
    global _gateway, _config_dir, _local_dir, _node_id
    _gateway = gateway
    _config_dir = config_dir
    _local_dir = local_dir
    _node_id = node_id

    mcps = []

    mcp_base = _make_mcp("boxagent", "/mcp/base")
    _register_schedule_tools(mcp_base)
    _register_session_tools(mcp_base)
    mcps.append(mcp_base)

    mcp_admin = _make_mcp("boxagent-admin", "/mcp/admin")
    _register_admin_tools(mcp_admin)
    mcps.append(mcp_admin)

    mcp_telegram = _make_mcp("boxagent-telegram", "/mcp/telegram")
    _register_telegram_tools(mcp_telegram)
    mcps.append(mcp_telegram)

    mcp_peer = _make_mcp("boxagent-peer", "/mcp/peer")
    _register_peer_tools(mcp_peer)
    mcps.append(mcp_peer)

    # Collect routes from all FastMCP apps into a single Starlette app.
    # Each FastMCP registers its route at its own streamable_http_path.
    from contextlib import asynccontextmanager
    from starlette.routing import Route

    routes: list[Route] = []
    for m in mcps:
        sub_app = m.streamable_http_app()
        routes.extend(sub_app.routes)

    # Shared lifespan: start/stop all session managers.
    @asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for m in mcps:
                await stack.enter_async_context(m.session_manager.run())
            yield

    import contextlib
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(_ContextMiddleware)
    return app
