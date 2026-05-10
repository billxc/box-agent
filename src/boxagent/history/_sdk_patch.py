"""Monkey patch for ``claude_agent_sdk`` to surface per-message metadata.

The SDK's ``SessionMessage`` (see ``claude_agent_sdk/types.py``) only carries
``type / uuid / session_id / message / parent_tool_use_id`` — the JSONL
transcript fields ``timestamp / cwd / gitBranch`` are dropped by
``_to_session_message`` during conversion. The web UI's transcript replay
needs at least ``timestamp`` for chronological display, and ``cwd /
gitBranch`` are cheap to forward at the same point.

We intercept the single conversion site
``claude_agent_sdk._internal.sessions._to_session_message`` and attach the
extra fields as plain attributes on the returned ``SessionMessage`` instance
(it's a regular ``@dataclass`` — no slots, not frozen).

Failure modes:
- SDK upgrade renames or removes ``_to_session_message``: import fails;
  we log a warning and leave callers to fall back to ``ts=0.0`` etc.
  ``test_sdk_patch.py`` will catch this in CI.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Sentinel module attribute so ``apply()`` is idempotent (re-importing
# ``boxagent.history`` shouldn't double-wrap).
_PATCHED = False


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return
    try:
        from claude_agent_sdk._internal import sessions as sdk_sessions
    except Exception as e:
        logger.warning("history: SDK monkey patch skipped (import failed): %s", e)
        return

    original = getattr(sdk_sessions, "_to_session_message", None)
    if original is None:
        logger.warning(
            "history: SDK monkey patch skipped — _to_session_message missing "
            "(SDK API changed?)",
        )
        return

    def patched(entry):
        message = original(entry)
        # Forward raw JSONL fields (kept as-is — ISO 8601 string for
        # timestamp; consumers parse). Best-effort: missing keys → None.
        try:
            message.timestamp = entry.get("timestamp")
            message.cwd = entry.get("cwd")
            message.git_branch = entry.get("gitBranch")
        except Exception:
            # Defensive — entry is always a dict in current SDK, but if the
            # contract changes we'd rather not break message conversion.
            pass
        return message

    sdk_sessions._to_session_message = patched
    _PATCHED = True
