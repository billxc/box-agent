"""Sessions search tool — list/search BoxAgent sessions across all backends."""

from __future__ import annotations

import logging

from boxagent.tools import ToolContext, boxagent_tool

logger = logging.getLogger(__name__)


@boxagent_tool(
    name="sessions_list",
    group="base",
    description=(
        "Search and list sessions (Claude CLI + BoxAgent history + Codex). "
        "Query syntax tokens (all optional, order-independent): "
        "'--all' to skip workspace filter; <keywords> for text search "
        "(multi-word AND); 'cwd:<substr>' fuzzy match path; "
        "'grep:<substr>' full-text search inside JSONL; '<N>d' last N days; "
        "'backend:<name>'; 'bot:<name>'; 'p<N>' page; "
        "'<hex-prefix>' lookup by id prefix (4+ hex chars). "
        "By default scoped to *workspace* arg (project dir)."
    ),
    schema={"query": str, "workspace": str},
)
async def sessions_list(args: dict, ctx: ToolContext) -> str:
    from boxagent.sessions import Storage
    from boxagent.sessions.browser import format_sessions_list

    storage = Storage(ctx.local_dir) if ctx.local_dir else None
    return format_sessions_list(
        query=args.get("query", ""),
        storage=storage,
        workspace=args.get("workspace", ""),
    )
