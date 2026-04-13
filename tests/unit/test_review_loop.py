"""Unit tests for the review loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from boxagent.channels.base import IncomingMessage
from boxagent.review_loop import ReviewLoopRunner, _CONVERGENCE_SIGNALS


# -- helpers --

def _make_runner(**overrides):
    cli = AsyncMock()
    cli.session_id = "sess_main"
    defaults = dict(
        cli_process=cli,
        channel=AsyncMock(),
        chat_id="123",
        workspace="/tmp/test",
        max_rounds=3,
        model="",
    )
    defaults.update(overrides)
    return ReviewLoopRunner(**defaults)


def _mock_author():
    """Return a mock author process whose send() streams text to the callback."""
    author = AsyncMock()

    async def mock_send(prompt, callback, model="", chat_id=""):
        await callback.on_stream("some content")

    author.send = AsyncMock(side_effect=mock_send)
    author.stop = AsyncMock()
    return author


# -- _is_converged --

class TestIsConverged:
    def test_positive_signals(self):
        for signal in _CONVERGENCE_SIGNALS:
            assert ReviewLoopRunner._is_converged(f"After review: {signal}.")

    def test_case_insensitive(self):
        assert ReviewLoopRunner._is_converged("NO ISSUES FOUND in the code")

    def test_negative(self):
        assert not ReviewLoopRunner._is_converged(
            "Issue 1 [major]: missing error handling"
        )

    def test_empty(self):
        assert not ReviewLoopRunner._is_converged("")


# -- _truncate --

class TestTruncate:
    def test_short_text_unchanged(self):
        assert ReviewLoopRunner._truncate("hello") == "hello"

    def test_long_text_truncated(self):
        text = "x" * 4000
        result = ReviewLoopRunner._truncate(text, max_len=100)
        assert len(result) < 200
        assert "truncated" in result
        assert "4000" in result

    def test_exact_boundary(self):
        text = "a" * 3000
        assert ReviewLoopRunner._truncate(text) == text


# -- full loop --

class TestReviewLoopRun:
    async def test_converges_on_first_round(self):
        channel = AsyncMock()
        runner = _make_runner(channel=channel)
        author = _mock_author()

        with (
            patch.object(runner, "_spawn_author", return_value=author),
            patch.object(runner, "_reviewer_review", return_value="No issues found."),
        ):
            await runner.run("test topic")

        # Should have: start msg, author round 1, review round 1, done msg
        assert channel.send_text.call_count == 4
        final_text = channel.send_text.call_args_list[-1][0][1]
        assert "converged" in final_text
        author.stop.assert_awaited_once()

    async def test_reaches_max_rounds(self):
        channel = AsyncMock()
        runner = _make_runner(channel=channel, max_rounds=2)
        author = _mock_author()

        with (
            patch.object(runner, "_spawn_author", return_value=author),
            patch.object(
                runner, "_reviewer_review", return_value="Issue 1 [major]: bad code"
            ),
        ):
            await runner.run("test topic")

        final_text = channel.send_text.call_args_list[-1][0][1]
        assert "max" in final_text.lower()
        author.stop.assert_awaited_once()

    async def test_author_stopped_on_error(self):
        """Author process is always stopped, even if an error occurs."""
        channel = AsyncMock()
        runner = _make_runner(channel=channel)
        author = _mock_author()
        author.send = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch.object(runner, "_spawn_author", return_value=author),
            pytest.raises(RuntimeError),
        ):
            await runner.run("test topic")

        author.stop.assert_awaited_once()

    async def test_no_topic_handled_by_router(self):
        """Router should send usage help when topic is empty."""
        from boxagent.router import Router

        cli = AsyncMock()
        cli.state = "idle"
        cli.session_id = None
        cli.supports_session_persistence = True
        cli.reset_session = AsyncMock()
        channel = AsyncMock()

        router = Router(
            cli_process=cli,
            channel=channel,
            allowed_users=[123],
            workspace="/tmp",
        )
        incoming = IncomingMessage(
            channel="telegram", chat_id="123", user_id="123", text="/review_loop",
        )
        await router.handle_message(incoming)

        text = channel.send_text.call_args[0][1]
        assert "Usage" in text
