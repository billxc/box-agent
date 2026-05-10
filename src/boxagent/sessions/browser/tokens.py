"""Token parser for the ``/sessions`` query DSL.

Recognized tokens:
  --all            show sessions from all projects (ignore cwd filter)
  pN               page number (1-based)
  Nd               time filter, last N days
  backend:X        substring match on entry.backend
  bot:X            substring match on entry.bot
  cwd:X            substring match on entry.projectPath
  grep_pattern:X           full-text search inside the session JSONL
  <hex prefix>     resolved later by the format layer (≥4 hex chars)
  <other>          collected as free-text query (multi-word AND, multi-field OR)
"""

from __future__ import annotations

import re


_RE_PAGE = re.compile(r"^p(\d+)$", re.IGNORECASE)
_RE_DAYS = re.compile(r"^(\d+)d$", re.IGNORECASE)
_RE_BACKEND = re.compile(r"^backend:(.+)$", re.IGNORECASE)
_RE_BOT = re.compile(r"^bot:(.+)$", re.IGNORECASE)
_RE_CWD = re.compile(r"^cwd:(.+)$", re.IGNORECASE)
_RE_GREP = re.compile(r"^grep_pattern:(.+)$", re.IGNORECASE)
_RE_HEX = re.compile(r"^[0-9a-f]{4,}$", re.IGNORECASE)


def parse_session_tokens(raw: str) -> dict:
    """Parse the argument string after ``/sessions``.

    Returns a dict with keys: page, days, backend, bot, cwd_search, grep_pattern,
    id_prefix, query, all.
    """
    tokens = raw.split()
    page = 1
    days: int | None = None
    backend = ""
    bot = ""
    cwd_search = ""
    grep_pattern = ""
    id_prefix = ""
    all_flag = False
    query_parts: list[str] = []

    page_set = False
    days_set = False

    for token in tokens:
        if token == "--all":
            all_flag = True
            continue

        m = _RE_PAGE.match(token)
        if m and not page_set:
            page = int(m.group(1))
            page_set = True
            continue

        m = _RE_DAYS.match(token)
        if m and not days_set:
            days = int(m.group(1))
            days_set = True
            continue

        m = _RE_BACKEND.match(token)
        if m and not backend:
            backend = m.group(1)
            continue

        m = _RE_BOT.match(token)
        if m and not bot:
            bot = m.group(1)
            continue

        m = _RE_CWD.match(token)
        if m and not cwd_search:
            cwd_search = m.group(1)
            continue

        m = _RE_GREP.match(token)
        if m and not grep_pattern:
            grep_pattern = m.group(1)
            continue

        # Hex prefix is checked later against actual sessions
        # For now just collect as query part
        query_parts.append(token)

    return {
        "page": page,
        "days": days,
        "backend": backend,
        "bot": bot,
        "cwd_search": cwd_search,
        "grep_pattern": grep_pattern,
        "id_prefix": id_prefix,
        "query": " ".join(query_parts),
        "all": all_flag,
    }
