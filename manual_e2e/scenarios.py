"""E2E scenarios. Each is a list of turns to send.

A scenario is a callable returning a sequence of (prompt, post_action)
tuples; ``post_action`` is an optional async fn run after the turn
completes (e.g. cancel, reset, sleep).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from boxagent.agent.protocol import AgentBackend


PostAction = Callable[[AgentBackend], Awaitable[None]] | None
Scenario = Callable[[], list[tuple[str, PostAction]]]


def hello() -> list[tuple[str, PostAction]]:
    """One-turn smoke test — does the backend stream any text at all?"""
    return [
        ("Reply with exactly: hello world. Nothing else.", None),
    ]


def multi_turn_recall() -> list[tuple[str, PostAction]]:
    """Two turns — does session continuity work? Backend must remember
    a word from turn 1 in turn 2."""
    return [
        ("Remember this word for me: pineapple. Just acknowledge.", None),
        ("What word did I ask you to remember?", None),
    ]


def tool_use_bash() -> list[tuple[str, PostAction]]:
    """Trigger a tool call. Should produce on_tool_call/on_tool_update events."""
    return [
        (
            "Run the shell command `echo hi-from-boxagent` and tell me the output.",
            None,
        ),
    ]


def tool_use_read_file() -> list[tuple[str, PostAction]]:
    """File read tool call."""
    return [
        (
            "Read the first 5 lines of /etc/hosts and tell me what's in them.",
            None,
        ),
    ]


def cancel_mid_turn() -> list[tuple[str, PostAction]]:
    """Long prompt, cancel after 2 seconds. Test that cancel reaches the backend."""
    async def _cancel_after_2s(backend: AgentBackend) -> None:
        # Driver waits for the turn to complete; we want to interrupt.
        # Schedule a cancel that fires concurrently. Returns immediately
        # so the driver moves on.
        await asyncio.sleep(2.0)
        await backend.cancel()

    return [
        (
            "Count slowly from 1 to 100, one number per line, with a brief comment "
            "for each. Do not rush.",
            _cancel_after_2s,
        ),
    ]


def error_recovery() -> list[tuple[str, PostAction]]:
    """Trigger an obvious tool error then ask a follow-up — does the backend recover?"""
    return [
        (
            "Run `ls /this-path-does-not-exist-xyz` and tell me what happened.",
            None,
        ),
        ("Now just say 'OK, recovered' to confirm you can still respond.", None),
    ]


SCENARIOS: dict[str, Scenario] = {
    "hello": hello,
    "multi_turn_recall": multi_turn_recall,
    "tool_use_bash": tool_use_bash,
    "tool_use_read_file": tool_use_read_file,
    "cancel_mid_turn": cancel_mid_turn,
    "error_recovery": error_recovery,
}
