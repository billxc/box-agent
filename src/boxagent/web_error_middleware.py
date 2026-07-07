"""把 handler 异常 / 5xx 打进 event log 的中间件（aiohttp + Starlette 两版）。

否则 handler 未处理的错误只走 Python `logging`，永远到不了 EventBus /
web UI / Telegram notifier。

- ``error_logging_middleware``（aiohttp 版）：InternalApiServer 仍是 aiohttp，用它。
- ``ErrorLoggingMiddleware``（Starlette 版）：Web UI server 已迁到 Starlette，用它。
两者行为对等：捕获 handler 异常 + 5xx 响应，写 ``Category.WEB_ERROR``。
"""
from __future__ import annotations

import traceback

from aiohttp import web
from starlette.middleware.base import BaseHTTPMiddleware

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


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """Starlette 版：与 aiohttp 版行为对等，把 handler 异常 / 5xx 打进 event log。"""

    async def dispatch(self, request, call_next):
        method = request.method
        path = request.url.path
        query = request.url.query
        try:
            response = await call_next(request)
        except Exception as e:
            log.error(
                Category.WEB_ERROR,
                f"{method} {path} -> 500 {type(e).__name__}: {e}",
                method=method, path=path, status=500, query=query,
                exception=type(e).__name__,
                traceback=traceback.format_exc(limit=20),
            )
            raise
        if response.status_code >= 500:
            log.error(
                Category.WEB_ERROR,
                f"{method} {path} -> {response.status_code}",
                method=method, path=path, status=response.status_code, query=query,
            )
        return response
