"""CopilotAgentHistory — wraps ``copilot.CopilotClient`` session APIs.

Copilot CLI keeps its own session store. The SDK exposes
``client.list_sessions()`` / ``client.get_session_metadata()`` /
``client.list_sessions(filter=...)``. Reading a transcript requires
attaching a (passive) session and pulling ``get_messages()``.

Lifecycle: this class spins up its own ``CopilotClient`` lazily and
stops it on ``close()``. It does NOT share the
``AgentSDKCopilot._SHARED_CLIENT`` because history reads are
independent of any active conversation and may happen before / after
any backend instance exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from boxagent.history.protocol import Message, ProjectInfo, SessionInfo

if TYPE_CHECKING:
    from copilot import CopilotClient

logger = logging.getLogger(__name__)


class CopilotAgentHistory:
    """``AgentHistory`` implementation for the Copilot SDK's session store."""

    def __init__(self, client: "CopilotClient | None" = None) -> None:
        self._client: CopilotClient | None = client
        self._owns_client = client is None

    async def _ensure_client(self) -> "CopilotClient":
        if self._client is not None:
            return self._client
        from copilot import CopilotClient

        self._client = CopilotClient()
        await self._client.start()
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.stop()
            except Exception as e:
                logger.warning("CopilotClient.stop in history close failed: %s", e)
            self._client = None

    # ── Public API ────────────────────────────────────────────────

    async def list_projects(self) -> list[ProjectInfo]:
        client = await self._ensure_client()
        try:
            metas = await client.list_sessions()
        except Exception as e:
            logger.warning("CopilotClient.list_sessions failed: %s", e)
            return []
        # Bucket by cwd → ProjectInfo
        buckets: dict[str, list] = {}
        for m in metas:
            cwd = self._cwd_of(m)
            buckets.setdefault(cwd, []).append(m)
        out: list[ProjectInfo] = []
        for cwd, items in buckets.items():
            last_ts = max((self._ts_of(m) for m in items), default=0.0)
            label = cwd.rstrip("/").rsplit("/", 1)[-1] or "(no cwd)"
            out.append(ProjectInfo(
                project_id=cwd or "(unknown)",
                label=label,
                cwd=cwd,
                session_count=len(items),
                last_ts=last_ts,
            ))
        out.sort(key=lambda p: p.last_ts, reverse=True)
        return out

    async def list_sessions(self, project_id: str) -> list[SessionInfo]:
        client = await self._ensure_client()
        try:
            metas = await client.list_sessions()
        except Exception as e:
            logger.warning("CopilotClient.list_sessions failed: %s", e)
            return []
        out: list[SessionInfo] = []
        for m in metas:
            cwd = self._cwd_of(m)
            if project_id and cwd != project_id:
                continue
            out.append(SessionInfo(
                session_id=str(getattr(m, "session_id", "")) or str(getattr(m, "id", "")),
                project_id=cwd,
                first_user=str(getattr(m, "first_prompt", "") or "")[:120],
                message_count=int(getattr(m, "message_count", 0) or 0),
                last_ts=self._ts_of(m),
                created_at=self._created_of(m),
                cwd=cwd,
                summary=str(getattr(m, "title", "") or getattr(m, "summary", "") or ""),
            ))
        out.sort(key=lambda s: s.last_ts, reverse=True)
        return out

    async def get_session_info(
        self, session_id: str, project_id: str = "",
    ) -> SessionInfo | None:
        client = await self._ensure_client()
        try:
            meta = await client.get_session_metadata(session_id)
        except Exception as e:
            logger.warning("get_session_metadata(%s) failed: %s", session_id, e)
            return None
        if meta is None:
            return None
        cwd = self._cwd_of(meta)
        return SessionInfo(
            session_id=session_id,
            project_id=cwd,
            first_user=str(getattr(meta, "first_prompt", "") or "")[:120],
            message_count=int(getattr(meta, "message_count", 0) or 0),
            last_ts=self._ts_of(meta),
            created_at=self._created_of(meta),
            cwd=cwd,
            summary=str(getattr(meta, "title", "") or getattr(meta, "summary", "") or ""),
        )

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        # The SDK exposes get_messages() on a CopilotSession instance, so
        # we have to attach (resume) the session then read its history.
        # We do NOT create a permission handler that approves anything —
        # this is a read-only attach; if the caller invokes anything that
        # triggers tools we let the SDK reject them.
        from copilot.session import PermissionHandler

        client = await self._ensure_client()
        try:
            session = await client.resume_session(
                session_id,
                on_permission_request=PermissionHandler.approve_all,
            )
        except Exception as e:
            logger.warning("resume_session(%s) failed: %s", session_id, e)
            return []
        try:
            events_or_coro = session.get_messages()
            # Some SDK versions return list directly, newer versions return a
            # coroutine — accept both.
            if asyncio.iscoroutine(events_or_coro):
                events = await events_or_coro
            else:
                events = events_or_coro
        except Exception as e:
            logger.warning("get_messages(%s) failed: %s", session_id, e)
            events = []
        finally:
            try:
                await session.disconnect()
            except Exception:
                pass
        return self._convert_events(events)

    # ── SDK shape coercion ────────────────────────────────────────

    @staticmethod
    def _cwd_of(meta: Any) -> str:
        for attr in ("cwd", "working_directory", "workspace"):
            v = getattr(meta, attr, None)
            if isinstance(v, str) and v:
                return v
        return ""

    @staticmethod
    def _ts_of(meta: Any) -> float:
        for attr in ("last_modified", "updated_at", "modified_at"):
            v = getattr(meta, attr, None)
            if isinstance(v, (int, float)):
                # SDK frequently exposes ms-since-epoch — heuristic divide
                # by 1000 when value looks too large.
                return float(v) / 1000.0 if v > 1e12 else float(v)
        return 0.0

    @staticmethod
    def _created_of(meta: Any) -> float:
        for attr in ("created_at", "created"):
            v = getattr(meta, attr, None)
            if isinstance(v, (int, float)):
                return float(v) / 1000.0 if v > 1e12 else float(v)
        return 0.0

    @staticmethod
    def _convert_events(events: list[Any]) -> list[Message]:
        # SessionEvent's data discriminator carries the role:
        # AssistantMessageData / UserMessageData / etc. We only surface
        # the user/assistant text; tool/permission events are intentionally
        # dropped (they're SDK-internal lifecycle, not transcript content).
        try:
            from copilot.generated.session_events import (
                AssistantMessageData,
                UserMessageData,
            )
        except ImportError:
            return []
        out: list[Message] = []
        for ev in events:
            data = getattr(ev, "data", None)
            if isinstance(data, AssistantMessageData):
                text = getattr(data, "content", "") or ""
                if text:
                    out.append(Message(role="assistant", text=text))
            elif isinstance(data, UserMessageData):
                text = getattr(data, "content", "") or getattr(data, "message", "") or ""
                if text:
                    out.append(Message(role="user", text=text))
        return out
