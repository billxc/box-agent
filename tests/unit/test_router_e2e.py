"""Black-box end-to-end tests for Router.

Router is treated as an opaque box — input is delivered through
MockChannel, output is observed on MockBackend.sends and
MockChannel.sent_texts / streams. No router internals (._compact_summaries,
.pool, etc.) are inspected.

If a behaviour can't be expressed at this contract surface, it doesn't
belong here; add a unit test instead.
"""

import asyncio

import pytest

from boxagent.transports.base import IncomingMessage
from boxagent.router import Router
from boxagent.testing.mocks import MockBackend, MockChannel


def _msg(text: str, *, chat_id: str = "1", user_id: str = "1") -> IncomingMessage:
    return IncomingMessage(
        channel="telegram", chat_id=chat_id, user_id=user_id, text=text,
    )


@pytest.fixture
def world(tmp_path):
    """A wired (channel, backend) pair backed by a real Router.

    The Router itself is hidden — tests only touch ``world.channel`` and
    ``world.backend``.
    """
    backend = MockBackend(session_id="sess_e2e", supports_session_persistence=True)
    channel = MockChannel()
    router = Router(
        backend=backend,
        channel=channel,
        allowed_users=[1],
        bot_name="e2e-bot",
        local_dir=tmp_path,
    )
    channel.on_message = router.handle_message

    class World:
        pass
    w = World()
    w.channel = channel
    w.backend = backend
    return w


# ── Basic turn ────────────────────────────────────────────────────────


async def test_single_turn_streams_backend_chunks_to_channel(world):
    world.backend.script(["Let me check.", " Here you go: foo"])

    await world.channel.deliver(_msg("hi"))

    # Backend saw the user text exactly once.
    assert [s.message for s in world.backend.sends] == ["hi"]

    # Channel received both stream chunks on a single stream lifecycle.
    assert len(world.channel.streams) == 1
    assert world.channel.streams[-1].chunks == ["Let me check.", " Here you go: foo"]
    assert world.channel.streams[-1].closed


async def test_multi_turn_each_message_creates_its_own_stream(world):
    world.backend.script(["one"])
    await world.channel.deliver(_msg("first"))

    world.backend.script(["two"])
    await world.channel.deliver(_msg("second"))

    assert [s.message for s in world.backend.sends] == ["first", "second"]
    assert len(world.channel.streams) == 2
    assert world.channel.streams[0].chunks == ["one"]
    assert world.channel.streams[1].chunks == ["two"]


# ── Auth ──────────────────────────────────────────────────────────────


async def test_unauthorized_user_never_reaches_backend(world):
    await world.channel.deliver(_msg("hi", user_id="999"))

    assert world.backend.sends == []
    assert any(
        "unauthorized" in t.lower() or "not allowed" in t.lower()
        for _chat, t in world.channel.sent_texts
    )


# ── Slash commands (black-box: backend untouched, channel sees reply) ─


@pytest.mark.parametrize("cmd", ["/status", "/help", "/start"])
async def test_slash_commands_do_not_dispatch_to_backend(world, cmd):
    await world.channel.deliver(_msg(cmd))

    assert world.backend.sends == []
    assert len(world.channel.sent_texts) == 1


# ── /compact: summary from one turn appears in next turn's prompt ─────


async def test_compact_summary_threads_into_next_turn(world):
    # First, /compact triggers a summarisation turn whose stream output
    # IS the summary.
    world.backend.script(["- discussed pineapples\n- decided on bananas"])
    await world.channel.deliver(_msg("/compact"))

    # Then a normal turn — backend.send must receive the summary in
    # append_system_prompt without us touching router internals.
    world.backend.script(["acknowledged"])
    await world.channel.deliver(_msg("what's next?"))

    last = world.backend.sends[-1]
    assert last.message == "what's next?"
    assert "discussed pineapples" in last.append_system_prompt


# ── /cancel mid-turn ──────────────────────────────────────────────────


async def test_cancel_during_long_turn_releases_backend(world):
    started = asyncio.Event()
    release = asyncio.Event()

    async def long_running(message, callback, **_):
        started.set()
        await release.wait()

    world.backend.script_handler(long_running)

    # Kick off the slow turn, then cancel it from the same channel.
    turn = asyncio.create_task(world.channel.deliver(_msg("long task")))
    await started.wait()

    # /cancel makes the backend's cancel() fire — which our handler
    # observes by checking cancel_count, then we release the turn.
    cancel_turn = asyncio.create_task(world.channel.deliver(_msg("/cancel")))
    # Give the cancel a moment to land.
    for _ in range(50):
        if world.backend.cancel_count >= 1:
            break
        await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(turn, cancel_turn)

    assert world.backend.cancel_count == 1
    assert any("cancel" in t.lower() for _chat, t in world.channel.sent_texts)


# ── Failure surface ──────────────────────────────────────────────────


async def test_failed_turn_is_visible_to_backend_observers(world):
    async def boom(message, callback, **_):
        await callback.on_error("backend exploded")

    world.backend.script_handler(boom)
    world.backend.fail_next_turn("backend exploded")

    await world.channel.deliver(_msg("trigger failure"))

    assert world.backend.last_turn_failed
    assert world.backend.last_turn_error == "backend exploded"


# ── Autocompact lifecycle notifications ───────────────────────────────


async def test_compact_lifecycle_events_surface_to_channel(world):
    """SDK autocompact "compacting" + "boundary" events become user notices.

    sdk_claude_process.py forwards SDK SystemMessage(subtype="status",
    status="compacting") and SystemMessage(subtype="compact_boundary",
    compact_metadata={...}) as callback.on_compact_event(...). The
    ChannelCallback turns those into channel.send_text so the user sees
    progress during the ~2-minute compact, plus a result summary.
    """
    async def emit_compact(message, callback, **_):
        await callback.on_compact_event("compacting")
        await callback.on_compact_event("compacted")
        await callback.on_compact_event("boundary", {
            "trigger": "auto",
            "pre_tokens": 712652,
            "post_tokens": 9588,
            "duration_ms": 165512,
        })
        await callback.on_stream("done")

    world.backend.script_handler(emit_compact)
    await world.channel.deliver(_msg("anything"))

    notice_texts = [t for _chat, t in world.channel.sent_texts]
    assert any("auto-compacting" in t for t in notice_texts), notice_texts
    boundary_notices = [t for t in notice_texts if "Compacted" in t]
    assert boundary_notices, notice_texts
    summary = boundary_notices[-1]
    assert "712k" in summary
    assert "9k" in summary
    assert "165s" in summary
    assert "auto" in summary
