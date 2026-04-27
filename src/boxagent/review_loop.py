"""Review loop: multi-agent adversarial review via Author + Reviewer roles."""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from boxagent.router.callback import TextCollector

logger = logging.getLogger(__name__)

# Signals that the reviewer found no issues (case-insensitive substring match).
_CONVERGENCE_SIGNALS = [
    "no issues found",
    "no issues",
    "no problems found",
    "no problems",
    "all good",
    "looks good",
    "lgtm",
]


@dataclass
class ReviewLoopRunner:
    """Orchestrates an Author <-> Reviewer adversarial loop.

    Both Author and Reviewer run as isolate ClaudeProcess instances so the
    main session context is never polluted.

    - Author: fork of the main session (``--resume <id> --fork-session``),
      inherits the full conversation history.
    - Reviewer: fresh isolate process with no prior history.
    """

    cli_process: object  # main session (used only to read session_id)
    channel: object  # messaging channel
    chat_id: str
    workspace: str
    max_rounds: int = 3
    model: str = ""

    @asynccontextmanager
    async def _typing(self):
        """Send typing indicator every 4s while the block runs."""
        async def _loop():
            try:
                while True:
                    try:
                        await self.channel.show_typing(self.chat_id)
                    except Exception:
                        pass
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            task.cancel()

    async def run(self, topic: str) -> None:
        """Execute the review loop for *topic*."""
        await self.channel.send_text(
            self.chat_id,
            f"Review Loop started (max {self.max_rounds} rounds)",
        )

        # Spawn the Author as a fork of the main session.
        author = self._spawn_author()
        try:
            # Round 1: Author generates initial content.
            async with self._typing():
                content = await self._author_generate(author, topic)
            await self.channel.send_text(
                self.chat_id,
                f"Round 1 - Author\n\n{self._truncate(content)}",
            )

            for round_num in range(1, self.max_rounds + 1):
                # Reviewer reviews current content.
                context = self._build_reviewer_context(topic, content, round_num)
                async with self._typing():
                    feedback = await self._reviewer_review(context)
                await self.channel.send_text(
                    self.chat_id,
                    f"Round {round_num} - Review\n\n{feedback}",
                )

                # Check convergence.
                if self._is_converged(feedback):
                    await self.channel.send_text(
                        self.chat_id,
                        f"Review Loop done ({round_num} round(s), converged)",
                    )
                    return

                # Author responds to feedback.
                async with self._typing():
                    response = await self._author_respond(author, feedback, round_num)
                await self.channel.send_text(
                    self.chat_id,
                    f"Round {round_num} - Revision\n\n{self._truncate(response)}",
                )
                content = response

            await self.channel.send_text(
                self.chat_id,
                f"Review Loop done (reached max {self.max_rounds} rounds)",
            )
        finally:
            await author.stop()

    # ------------------------------------------------------------------
    # Author helpers (forked isolate session)
    # ------------------------------------------------------------------

    def _spawn_author(self):
        """Create an Author process that forks the main session."""
        from boxagent.agent.claude_process import ClaudeProcess

        session_id = getattr(self.cli_process, "session_id", None)

        proc = ClaudeProcess(
            workspace=self.workspace,
            session_id=session_id,
            yolo=True,
            fork_session=True,
        )
        proc.start()
        return proc

    async def _author_generate(self, author, topic: str) -> str:
        prompt = (
            "Please generate content for the following topic. "
            "Output the content directly without extra commentary.\n\n"
            f"Topic: {topic}"
        )
        collector = TextCollector()
        await author.send(prompt, collector, model=self.model)
        return collector.text.strip()

    async def _author_respond(self, author, feedback: str, round_num: int) -> str:
        prompt = (
            f"You received round-{round_num} review feedback. Please:\n"
            "1. Evaluate each issue: accept (and fix) or reject (with reason).\n"
            "2. Output the complete revised content.\n\n"
            f"--- Review feedback ---\n{feedback}"
        )
        collector = TextCollector()
        await author.send(prompt, collector, model=self.model)
        return collector.text.strip()

    # ------------------------------------------------------------------
    # Reviewer helper (fresh isolate process)
    # ------------------------------------------------------------------

    def _build_reviewer_context(self, topic: str, content: str, round_num: int) -> str:
        return (
            "You are a strict technical reviewer. "
            "You have full access to the workspace — use tools to read files, "
            "grep code, and verify claims made by the author.\n\n"
            f"Original task: {topic}\n\n"
            "The author produced the following output:\n"
            f"--- Author output (round {round_num}) ---\n{content}\n"
            "--- End of author output ---\n\n"
            "Review the author's work by:\n"
            "1. Reading the actual files in the workspace to verify correctness\n"
            "2. Checking for bugs, edge cases, and missed requirements\n"
            "3. Validating that the implementation matches the original task\n\n"
            "For each issue found, state:\n"
            "1. Severity (critical / major / minor)\n"
            "2. Description\n"
            "3. Why it matters\n"
            "4. Suggested fix direction\n\n"
            'If there are no issues, say "No issues found".'
        )

    async def _reviewer_review(self, prompt: str) -> str:
        from boxagent.agent.claude_process import ClaudeProcess

        proc = ClaudeProcess(
            workspace=self.workspace,
            yolo=True,
        )
        proc.start()
        try:
            collector = TextCollector()
            await proc.send(prompt, collector, model=self.model)
            return collector.text.strip()
        finally:
            await proc.stop()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _is_converged(feedback: str) -> bool:
        lower = feedback.lower()
        return any(s in lower for s in _CONVERGENCE_SIGNALS)

    @staticmethod
    def _truncate(text: str, max_len: int = 3000) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"\n\n... (truncated, {len(text)} chars total)"
