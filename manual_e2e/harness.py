"""Recording callback + helpers for manual E2E runs.

``RecordingCallback`` implements ``AgentCallback`` and prints every event
as it arrives, with timestamps relative to turn start. After a turn it
also keeps a structured record so the driver can dump a summary at the end.

We intentionally don't assert on anything — the whole point is to let a
human (or AI looking at the log) judge whether the backend behaved
correctly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


# ANSI colours so the log is readable in a terminal. Falls back gracefully
# when piped to a file (escape codes are inert there too).
class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BOLD = "\033[1m"


@dataclass
class TurnRecord:
    """One turn's worth of events, kept for end-of-run summary."""

    prompt: str
    started_at: float = 0.0
    ended_at: float = 0.0
    text_chunks: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_updates: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def assistant_text(self) -> str:
        return "".join(self.text_chunks)

    @property
    def duration(self) -> float:
        return (self.ended_at or time.time()) - self.started_at


class RecordingCallback:
    """``AgentCallback`` impl that prints every event and records it.

    Pretty-printing format::

        [+0.42s]  STREAM   "Hello"
        [+1.05s]  TOOL ▶   Bash  input={"command": "ls"}
        [+1.30s]  TOOL ✓   Bash  result="a.txt\\nb.txt"
        [+1.31s]  ERROR    Some error message
    """

    def __init__(self, *, label: str = "", verbose: bool = True) -> None:
        self.label = label
        self.verbose = verbose
        self.record = TurnRecord(prompt="")

    def begin_turn(self, prompt: str) -> None:
        self.record = TurnRecord(prompt=prompt, started_at=time.time())
        if self.verbose:
            self._println(C.BOLD + f"━━━ TURN START ({self.label}) ━━━" + C.RESET)
            self._println(C.DIM + f"  prompt: {self._truncate(prompt, 200)}" + C.RESET)

    def end_turn(self) -> None:
        self.record.ended_at = time.time()
        if self.verbose:
            self._println(
                C.BOLD
                + f"━━━ TURN END  ({self.record.duration:.2f}s, "
                + f"{len(self.record.text_chunks)} chunks, "
                + f"{len(self.record.tool_calls)} tool calls"
                + (f", {len(self.record.errors)} errors" if self.record.errors else "")
                + ") ━━━"
                + C.RESET
            )

    # ── AgentCallback protocol ──

    async def on_stream(self, text: str) -> None:
        self.record.text_chunks.append(text)
        if self.verbose:
            self._println(self._stamp() + C.GREEN + "STREAM   " + C.RESET + repr(text))

    async def on_tool_call(self, name: str, input: dict, result: str, tool_id: str = "") -> None:
        self.record.tool_calls.append({
            "name": name, "input": input, "result": result, "tool_id": tool_id,
        })
        if self.verbose:
            self._println(
                self._stamp()
                + C.YELLOW + f"TOOL ✓   {name}" + C.RESET
                + f"  input={self._compact(input)}"
            )
            self._println(
                "          " + C.DIM
                + f"result={self._truncate(result, 200)}"
                + C.RESET
            )

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self.record.tool_updates.append({
            "tool_call_id": tool_call_id, "title": title,
            "status": status, "input": input, "output": output,
        })
        if self.verbose:
            arrow = {"in_progress": "▶", "completed": "✓", "failed": "✗"}.get(status or "", "•")
            self._println(
                self._stamp()
                + C.YELLOW + f"TOOL {arrow}   {title}" + C.RESET
                + (f"  status={status}" if status else "")
            )

    async def on_error(self, error: str) -> None:
        self.record.errors.append(error)
        if self.verbose:
            self._println(self._stamp() + C.RED + "ERROR    " + C.RESET + error)

    async def on_file(self, path: str, caption: str = "") -> None:
        if self.verbose:
            self._println(self._stamp() + C.MAGENTA + "FILE     " + C.RESET + f"{path} ({caption})")

    async def on_image(self, path: str, caption: str = "") -> None:
        if self.verbose:
            self._println(self._stamp() + C.MAGENTA + "IMAGE    " + C.RESET + f"{path} ({caption})")

    # ── Internals ──

    def _stamp(self) -> str:
        elapsed = time.time() - self.record.started_at
        return C.DIM + f"[+{elapsed:5.2f}s]  " + C.RESET

    @staticmethod
    def _println(s: str) -> None:
        print(s, flush=True)

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        s = s.replace("\n", "\\n")
        return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"

    @staticmethod
    def _compact(v: Any) -> str:
        try:
            return json.dumps(v, ensure_ascii=False, default=str)[:200]
        except Exception:
            return str(v)[:200]


def banner(text: str) -> None:
    line = "═" * (len(text) + 4)
    print(f"\n{C.CYAN}{line}{C.RESET}")
    print(f"{C.CYAN}║ {C.BOLD}{text}{C.RESET}{C.CYAN} ║{C.RESET}")
    print(f"{C.CYAN}{line}{C.RESET}\n")
