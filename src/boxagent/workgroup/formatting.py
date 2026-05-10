"""Pure formatting helpers used by both WorkgroupManager and HeartbeatManager.

Lives in its own module so heartbeat.py can import it at top level
(workgroup/manager imports heartbeat, so the reverse direction needs to
avoid the cycle).
"""

from __future__ import annotations

import re
import time


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
