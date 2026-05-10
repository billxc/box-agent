"""Pure formatting helpers used by both WorkgroupManager and HeartbeatManager.

Lives in its own module so heartbeat.py can import it at top level
(workgroup/manager imports heartbeat, so the reverse direction needs to
avoid the cycle).
"""

from __future__ import annotations

import re
import time


# ── chat_id helpers ────────────────────────────────────────────────


_SPECIALIST_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def specialist_chat_id(specialist_name: str) -> str:
    """The virtual chat_id under which a specialist's pool/transcripts live.

    Single source of truth for the ``wg:<name>`` namespace; previously
    duplicated in 6 places. Names are validated by ``validate_specialist_name``
    at create time so the format is safe (no ``:`` collisions).
    """
    return f"wg:{specialist_name}"


def validate_specialist_name(name: str) -> str | None:
    """Return error message if ``name`` is invalid for a specialist, else None.

    Allowed: 1–31 chars, lowercase letters / digits / underscores / hyphens,
    must start with a letter or digit. Rejects names that would collide with
    the ``wg:`` chat_id namespace or contain reserved characters.
    """
    if not isinstance(name, str) or not name:
        return "specialist name must be a non-empty string"
    if not _SPECIALIST_NAME_RE.match(name):
        return (
            f"specialist name {name!r} is invalid — must be 1–31 chars of "
            "lowercase letters / digits / underscores / hyphens, "
            "starting with a letter or digit"
        )
    return None


# ── display ────────────────────────────────────────────────────────


def format_running_tasks(running_tasks: list[dict] | None) -> str:
    """Format running tasks into a display block. Used by context and heartbeat."""
    if not running_tasks:
        return "No specialist tasks currently running."
    lines = ["Currently running specialist tasks:"]
    for t in running_tasks:
        elapsed = ""
        started = t.get("started_at", 0)
        if started:
            secs = int(time.time() - started)
            mins, s = divmod(secs, 60)
            elapsed = f" (running {mins}m {s}s)"
        active = " [active]" if t.get("active") else " [queued]"
        lines.append(f"  - {t.get('task_id', '?')}: {t.get('target', '?')}{elapsed}{active}")
    return "\n".join(lines)


def extract_specialist_response(text: str) -> str:
    """Extract content from <specialist_response> tags. Falls back to raw text."""
    m = re.search(r"<specialist_response>(.*?)</specialist_response>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()
