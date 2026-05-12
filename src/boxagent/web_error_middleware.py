"""aiohttp middleware that funnels handler exceptions / 5xx into the event log.

Without this, aiohttp logs unhandled errors only via Python's `logging`
module, which never reaches our EventBus / web UI / Telegram notifier.
Install on every Application we own (web UI, internal API, MCP, cluster
RPC routes app).
"""
from __future__ import annotations

import traceback

from aiohttp import web

from boxagent.log import Category, log


@web.middleware
async def error_logging_middleware(request: web.Request, handler):
    method = request.method
    path = request.path
    query = request.query_string
    try:
        response = await handler(request)
    except web.HTTPException as e:
        if e.status >= 500:
            log.error(
                Category.WEB_ERROR,
                f"{method} {path} -> {e.status} {e.reason or ''}".strip(),
                method=method, path=path, status=e.status, query=query,
            )
        raise
    except Exception as e:
        log.error(
            Category.WEB_ERROR,
            f"{method} {path} -> 500 {type(e).__name__}: {e}",
            method=method, path=path, status=500, query=query,
            exception=type(e).__name__,
            traceback=traceback.format_exc(limit=20),
        )
        raise
    if response.status >= 500:
        log.error(
            Category.WEB_ERROR,
            f"{method} {path} -> {response.status}",
            method=method, path=path, status=response.status, query=query,
        )
    return response
