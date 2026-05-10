"""``AgentHistory`` Protocol — read-only access to one backend's session
history (transcript files / SDK session store).

Distinct from ``boxagent.sessions``:

- ``boxagent.sessions`` owns BoxAgent-specific mappings: ``bot_id:chat_id``
  → ``session_id``, the ``SessionPool`` that lends backends to chats, etc.
- ``boxagent.history`` is read-only: it lists projects / sessions and
  loads transcripts that the agent backend wrote on its own (e.g.
  ``~/.claude/projects/`` for Claude Code, ``~/.codex/sessions/`` for
  Codex CLI, the Copilot SDK's session store).

Each concrete history (Claude / Codex / Copilot) implements this
Protocol over its native storage. The web UI's resume picker, the
``sessions_list`` tool, and the Router's ``/resume`` command all go
through the factory in :mod:`boxagent.history.factory`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ProjectInfo:
    """Top-level grouping of sessions.

    For Claude this is one ``~/.claude/projects/<encoded>`` directory.
    For Codex / Copilot 'project' is a working-directory grouping.

    Attributes:
        project_id: Stable opaque id callers pass back to ``list_sessions``.
            Backend-specific shape (Claude: encoded dir name; Codex: cwd
            path; Copilot: cwd path).
        label: Short human-readable name (typically the basename).
        cwd: Resolved working directory, when available.
        session_count: Number of sessions in this project.
        last_ts: Last activity in this project, unix seconds.
    """

    project_id: str
    label: str
    cwd: str = ""
    session_count: int = 0
    last_ts: float = 0.0


@dataclass
class SessionInfo:
    """One session in one project.

    Attributes:
        session_id: Backend's session UUID (or thread_id, etc.).
        project_id: Owning project's id.
        first_user: First user message preview (truncated).
        message_count: Approximate message count (best effort, may
            over-count or be 0 if backend doesn't expose cheaply).
        last_ts: Last activity, unix seconds.
        created_at: Creation time, unix seconds. 0 if unknown.
        cwd: Working directory at session time.
        summary: Backend-supplied display title (Claude SDK exposes this).
        custom_title: User/AI-set custom title, when supported.
        git_branch: Git branch the session ran in, when known.
        tag: User-set tag, when supported.
    """

    session_id: str
    project_id: str = ""
    first_user: str = ""
    message_count: int = 0
    last_ts: float = 0.0
    created_at: float = 0.0
    cwd: str = ""
    summary: str = ""
    custom_title: str | None = None
    git_branch: str | None = None
    tag: str | None = None


@dataclass
class Message:
    """Normalised transcript record.

    Used by web UI's transcript replay and ``sessions_list`` previews.
    Roles:

    - ``user`` / ``assistant``: a text turn — ``text`` populated.
    - ``tool_call``: model invoked a tool — ``tool_id`` / ``name`` /
      ``args`` populated.
    - ``tool_result``: tool finished — ``tool_id`` / ``ok`` / ``summary``
      / ``error`` populated.
    - ``skill_output``: model emitted skill output (Claude-specific
      heuristic — a user message immediately following a tool result).

    Backends without per-message timestamps fill ``ts=0.0`` and the
    caller relies on chronological return order.
    """

    role: str  # "user" | "assistant" | "tool_call" | "tool_result" | "skill_output"
    text: str = ""
    ts: float = 0.0
    # tool_call fields
    tool_id: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    # tool_result fields
    ok: bool | None = None
    summary: str = ""
    error: str = ""
    # Per-record context (populated by ClaudeAgentHistory; other backends
    # may leave blank). cwd is the working directory at the time of the
    # turn; git_branch is the branch name from the JSONL transcript.
    cwd: str = ""
    git_branch: str = ""


@runtime_checkable
class AgentHistory(Protocol):
    """Read-only view of one backend's session storage.

    All methods are async — Claude's SDK reads are blocking under the
    hood (we wrap them in ``asyncio.to_thread``); Copilot's are natively
    async. Codex reads its own jsonl files synchronously but we keep the
    surface async for symmetry.
    """

    async def list_projects(self) -> list[ProjectInfo]:
        """All projects with at least one session, newest activity first."""
        ...

    async def list_sessions(self, project_id: str) -> list[SessionInfo]:
        """All sessions in one project, newest activity first."""
        ...

    async def get_session_info(
        self, session_id: str, project_id: str = "",
    ) -> SessionInfo | None:
        """Look up a single session by id. ``project_id`` is a hint
        (skips global scan when known); ``None`` if not found."""
        ...

    async def read_messages(
        self, session_id: str, project_id: str = "",
    ) -> list[Message]:
        """Full transcript for one session, in chronological order."""
        ...
