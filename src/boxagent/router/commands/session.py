"""Session commands — touch the per-chat backend / pool / storage state.

These are the commands that mutate "what session is the user in" — pool
session-id swaps, workspace switches, backend kind changes, conversation
compaction. Anything that resets, resumes, or rebinds session state.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from boxagent.router.callback import TextCollector
from boxagent.router.commands.registry import CommandCategory, command

if TYPE_CHECKING:
    from boxagent.router.core import Router
    from boxagent.transports.base import Channel, IncomingMessage


@command("/new", help="Start a fresh conversation", category=CommandCategory.SESSION)
async def cmd_new(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    chat_id = msg.chat_id
    if router.pool:
        router.pool.clear_session(chat_id)
    else:
        await router._reset_backend_session()
    router._compact_summaries.pop(chat_id, None)
    router._resume_contexts.pop(chat_id, None)
    if router.storage:
        router.storage.clear_session(router.bot_name, chat_id=chat_id)
    await channel.send_text(chat_id, "Started a fresh conversation.")


@command("/cancel", help="Cancel the current running task", category=CommandCategory.SESSION)
async def cmd_cancel(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    chat_id = msg.chat_id
    if router.pool:
        active = router.pool.get_active(chat_id)
        if active:
            await active.cancel()
            await channel.send_text(chat_id, "Cancelled current task.")
        else:
            await channel.send_text(chat_id, "No active task to cancel.")
    else:
        await router.backend.cancel()
        await channel.send_text(chat_id, "Cancelled current task.")


@command("/resume", help="List or restore a previous session", category=CommandCategory.SESSION)
async def cmd_resume(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    if not router.storage:
        await channel.send_text(msg.chat_id, "Resume history is unavailable (storage is disabled).")
        return

    arg = msg.text.strip().partition(" ")[2].strip()

    from boxagent.sessions.browser import _load_all_unified_sessions
    all_sessions = _load_all_unified_sessions(
        storage=router.storage, workspace=router.workspace,
    )

    if not arg:
        await _resume_list(router, msg, channel, all_sessions)
        return

    target = next(
        (e for e in all_sessions if str(e.get("sessionId", "")) == arg),
        None,
    )
    if target is None:
        await channel.send_text(
            msg.chat_id,
            f"Resume target not found: `{arg}`. Send `/resume` to list available sessions.",
        )
        return

    await _do_resume_native(router, msg, channel, target)


async def _resume_list(
    router: "Router",
    msg: "IncomingMessage",
    channel: "Channel",
    sessions: list[dict[str, object]],
) -> None:
    if not sessions:
        await channel.send_text(msg.chat_id, "No saved sessions found.")
        return

    # Group by backend, keep up to 10 per group
    groups: dict[str, list[dict[str, object]]] = {}
    for entry in sessions:
        backend = str(entry.get("backend", "")) or "other"
        groups.setdefault(backend, []).append(entry)

    lines = ["**Resume Sessions**"]
    buttons = []
    idx = 0
    for backend in sorted(groups):
        lines.append(f"\n**{backend}**")
        for entry in groups[backend][:10]:
            idx += 1
            session_id = str(entry.get("sessionId", ""))
            modified_ts = entry.get("modified_ts")
            time_str = ""
            if isinstance(modified_ts, int | float) and modified_ts:
                time_str = time.strftime("%m-%d %H:%M", time.localtime(modified_ts))
            preview = entry.get("summary") or entry.get("firstPrompt") or entry.get("preview") or ""
            preview_text = ""
            if isinstance(preview, str) and preview:
                preview_text = f" — {preview[:60]}"
            short_id = session_id[:8]
            project = entry.get("project", "")
            ws_label = f" `{project}`" if project else ""
            lines.append(f"{idx}. `{short_id}` {time_str}{ws_label}{preview_text}")
            btn_label = f"{idx}. {time_str}"
            if isinstance(preview, str) and preview:
                btn_label += f" {preview[:28]}"
            buttons.append((btn_label, f"/resume {session_id}"))
    text = "\n".join(lines)
    send_with_buttons = getattr(channel, "send_text_with_inline_keyboard", None)
    if send_with_buttons is not None:
        await send_with_buttons(msg.chat_id, text, buttons)
    else:
        await channel.send_text(msg.chat_id, text)


async def _do_resume_native(
    router: "Router",
    msg: "IncomingMessage",
    channel: "Channel",
    entry: dict[str, object],
) -> None:
    chat_id = msg.chat_id
    target_session_id = str(entry["sessionId"])
    restored_workspace = str(entry.get("projectPath", "")) if entry.get("projectPath") else ""
    restored_model = str(entry.get("model", "")) if entry.get("model") else ""

    if router.pool:
        router.pool.set_session_id(chat_id, target_session_id)
        if restored_workspace:
            router.pool.set_workspace(chat_id, restored_workspace)
        if restored_model:
            router.pool.set_model(chat_id, restored_model)
    else:
        await router._reset_backend_session()
        router.backend.session_id = target_session_id
    router._compact_summaries.pop(chat_id, None)
    router._resume_contexts.pop(chat_id, None)
    if router.storage is not None:
        router.storage.save_session(router.bot_name, target_session_id, chat_id=chat_id)

    info_parts = [f"Resumed session `{target_session_id[:8]}`"]
    if restored_workspace:
        info_parts.append(f"workspace: `{restored_workspace}`")
    if restored_model:
        info_parts.append(f"model: `{restored_model}`")
    await channel.send_text(chat_id, "\n".join(info_parts))


@command("/model", help="Show or switch model (e.g. /model sonnet)", category=CommandCategory.SESSION)
async def cmd_model(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Show or switch the model for this chat."""
    chat_id = msg.chat_id
    parts = msg.text.strip().split(maxsplit=1)

    if router.pool:
        current = router.pool.get_model(chat_id) or "default"
    else:
        current = getattr(router.backend, "model", "") or "default"

    if len(parts) < 2:
        await channel.send_text(chat_id, f"Current model: {current}")
        return

    new_model = parts[1].strip()
    if router.pool:
        router.pool.set_model(chat_id, new_model)
    else:
        router.backend.model = new_model
    await channel.send_text(chat_id, f"Model switched: {current} → {new_model}")


@command("/cd", help="Show or switch workspace (e.g. /cd ~/projects)", category=CommandCategory.SESSION)
async def cmd_cd(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Show or switch the working directory for this chat."""
    chat_id = msg.chat_id
    parts = msg.text.strip().split(maxsplit=1)

    if router.pool:
        current = router.pool.get_workspace(chat_id) or "(not set)"
    else:
        current = router.workspace or "(not set)"

    if len(parts) < 2:
        await channel.send_text(chat_id, f"Current workspace: {current}")
        return

    new_path = os.path.expanduser(parts[1].strip())
    if not os.path.isdir(new_path):
        await channel.send_text(chat_id, f"Directory not found: {new_path}")
        return

    new_path = os.path.realpath(new_path)
    if router.pool:
        router.pool.set_workspace(chat_id, new_path)
        router.pool.clear_session(chat_id)
    else:
        router.backend.workspace = new_path
        router.workspace = new_path
        await router._reset_backend_session()
    router._compact_summaries.pop(chat_id, None)
    router._resume_contexts.pop(chat_id, None)
    if router.storage:
        router.storage.clear_session(router.bot_name, chat_id=chat_id)
    await channel.send_text(chat_id, f"Workspace switched: {current} → {new_path}")


@command(
    "/backend",
    help="Show or switch AI backend (claude-cli/codex-cli/agent-sdk-*)",
    category=CommandCategory.SESSION,
)
async def cmd_backend(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Show or switch the AI backend."""
    from boxagent.agent.backend_factory import create_backend
    from boxagent.agent.protocol import BACKEND_KINDS
    from boxagent.config import BotConfig

    parts = msg.text.strip().split(maxsplit=1)
    valid = sorted(BACKEND_KINDS)

    if len(parts) < 2:
        await channel.send_text(
            msg.chat_id,
            f"Current backend: {router.ai_backend}\nAvailable: {', '.join(valid)}",
        )
        return

    new_kind = parts[1].strip()
    if new_kind not in BACKEND_KINDS:
        await channel.send_text(
            msg.chat_id,
            f"Unknown backend: {new_kind}\nAvailable: {', '.join(valid)}",
        )
        return

    if new_kind == router.ai_backend:
        await channel.send_text(msg.chat_id, f"Already using {new_kind}.")
        return

    old_kind = router.ai_backend
    old_backend = router.backend

    bot_config = BotConfig(
        name=router.bot_name,
        ai_backend=new_kind,
        workspace=getattr(old_backend, "workspace", router.workspace),
        model=getattr(old_backend, "model", "") or "",
        agent=getattr(old_backend, "agent", "") or "",
        yolo=bool(getattr(old_backend, "yolo", False)),
    )
    await old_backend.stop()
    new_backend = create_backend(bot_config, session_id=None)

    new_backend.start()
    router.backend = new_backend
    router.ai_backend = new_kind
    router._compact_summaries.clear()
    router._resume_contexts.clear()
    if router.storage:
        router.storage.clear_session(router.bot_name, chat_id=msg.chat_id)
    if router.on_backend_switched:
        await router.on_backend_switched(router.bot_name, new_backend, new_kind)
    await channel.send_text(msg.chat_id, f"Backend switched: {old_kind} → {new_kind}")


@command("/compact", help="Summarize and start a new session with context", category=CommandCategory.SESSION)
async def cmd_compact(router: "Router", msg: "IncomingMessage", channel: "Channel") -> None:
    """Summarize current conversation, reset session, carry summary forward."""
    chat_id = msg.chat_id

    sid = (
        router.pool.get_session_id(chat_id) if router.pool
        else getattr(router.backend, "session_id", None)
    )
    if not sid:
        await channel.send_text(chat_id, "No active session to compact.")
        return

    await channel.send_text(chat_id, "Compacting conversation...")

    user_hint = msg.text.strip().partition(" ")[2].strip()

    summary_prompt = (
        "Please provide a concise summary of our entire conversation so far. "
        "Include: key topics discussed, decisions made, important context, "
        "and any pending tasks. Format as bullet points. "
        "This summary will be used to continue in a new session."
    )
    if user_hint:
        summary_prompt += f"\n\nAdditional instructions: {user_hint}"

    collector = TextCollector()
    await channel.show_typing(chat_id)
    try:
        env = router._build_env(msg)
        async with router._acquire_proc(chat_id) as backend:
            await backend.send(summary_prompt, collector, env=env)
    except Exception as e:
        await channel.send_text(chat_id, f"Failed to generate summary: {e}")
        return

    summary = collector.text.strip()
    if not summary:
        await channel.send_text(chat_id, "Failed to generate summary (empty response).")
        return

    if router.pool is not None:
        router.pool.clear_session(chat_id)
    else:
        await router._reset_backend_session()
    if router.storage:
        # preserve_chain so the next save_session keeps the old sid in
        # previous_session_ids — history readers (web UI, A-path walker)
        # can still surface pre-compact transcript content.
        router.storage.clear_session(router.bot_name, chat_id=chat_id, preserve_chain=True)

    router._resume_contexts.pop(chat_id, None)
    router._compact_summaries[chat_id] = summary

    await channel.send_text(
        chat_id,
        f"Session compacted. Summary:\n\n{summary}\n\n"
        "Next message will start a new session with this context.",
    )
