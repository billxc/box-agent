"""Build a :class:`SessionInfo` snapshot for one ``session_id``.

Pure disk read — does not depend on chats, pools, or live backend
instances. The backend-specific transcript file is the source of truth
for ``message_count``, ``last_ts``, and the most recent assistant turn's
``usage`` block.
"""

from __future__ import annotations

import logging

from boxagent.agent.session_info import SessionInfo
from boxagent.history.factory import get_history, supported_backends

logger = logging.getLogger(__name__)


# Default context window when we can't otherwise tell. 1M is generous —
# it covers Sonnet 4.5's 1M variant and degrades gracefully for 200k
# models (the percentage just looks small). Override per-model in
# CONTEXT_WINDOWS below as needed.
DEFAULT_CONTEXT_WINDOW = 1_000_000

CONTEXT_WINDOWS: dict[str, int] = {
    # Add overrides as we learn them, e.g.:
    # "claude-haiku-4-5": 200_000,
}


def context_window_for(model: str) -> int:
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    for prefix, window in CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return DEFAULT_CONTEXT_WINDOW


def context_used_from_usage(usage: dict[str, int] | None) -> int:
    """Total prompt-side tokens for the last turn = input + cache_creation
    + cache_read. This is what the model actually saw on the wire."""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


async def build_session_info(
    *,
    session_id: str,
    backend_kind: str,
    model: str = "",
    workspace: str = "",
) -> SessionInfo:
    info = SessionInfo(
        session_id=session_id,
        backend_kind=backend_kind,
        model=model,
        workspace=workspace,
    )

    if session_id and backend_kind in supported_backends():
        try:
            history = get_history(backend_kind)
            disk = await history.get_session_info(session_id, workspace)
            if disk is not None:
                info.message_count = disk.message_count
                info.last_ts = disk.last_ts
                if not info.workspace and disk.cwd:
                    info.workspace = disk.cwd
            info.last_turn_usage = await _read_last_usage_from_disk(
                backend_kind, session_id, workspace,
            )
        except Exception as e:
            logger.debug("history lookup failed for %s/%s: %s",
                         backend_kind, session_id, e)

    info.context_window = context_window_for(info.model)
    info.context_used = context_used_from_usage(info.last_turn_usage)
    return info


async def _read_last_usage_from_disk(
    backend_kind: str, session_id: str, project_id: str,
) -> dict[str, int] | None:
    if backend_kind in ("claude-cli", "agent-sdk-claude"):
        return await _claude_last_usage(session_id, project_id)
    if backend_kind == "codex-cli":
        return await _codex_last_usage(session_id, project_id)
    return None


async def _claude_last_usage(
    session_id: str, project_id: str,
) -> dict[str, int] | None:
    import asyncio

    def _read() -> dict[str, int] | None:
        try:
            from claude_agent_sdk import get_session_messages
        except Exception:
            return None
        try:
            messages = get_session_messages(session_id, project_id or None)
        except Exception:
            return None
        for entry in reversed(messages):
            raw = getattr(entry, "message", None)
            if not isinstance(raw, dict):
                continue
            if raw.get("role") != "assistant":
                continue
            usage = raw.get("usage")
            if isinstance(usage, dict):
                return _normalize(usage)
        return None

    return await asyncio.to_thread(_read)


async def _codex_last_usage(
    session_id: str, project_id: str,
) -> dict[str, int] | None:
    import asyncio
    import json
    from pathlib import Path

    def _read() -> dict[str, int] | None:
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.exists():
            return None
        match: Path | None = None
        for path in sessions_dir.rglob(f"rollout-*-{session_id}.jsonl"):
            match = path
            break
        if match is None:
            return None
        try:
            lines = match.read_text().splitlines()
        except Exception:
            return None
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                info_block = payload.get("info") or {}
                usage = info_block.get("last_token_usage") or info_block.get("total_token_usage")
                if isinstance(usage, dict):
                    return _normalize_codex(usage)
        return None

    return await asyncio.to_thread(_read)


def _normalize(usage: dict) -> dict[str, int]:
    keys = (
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    )
    return {k: int(usage[k]) for k in keys if isinstance(usage.get(k), (int, float))}


def _normalize_codex(usage: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for src, dst in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cached_input_tokens", "cache_read_input_tokens"),
    ):
        value = usage.get(src)
        if isinstance(value, (int, float)):
            out[dst] = int(value)
    return out
