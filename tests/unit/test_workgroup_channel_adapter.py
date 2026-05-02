"""Unit tests for workgroup channel adapters."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from boxagent.config import SpecialistConfig, WorkgroupConfig
from boxagent.workgroup.channel_adapter import (
    DiscordWorkgroupAdapter,
    NullWorkgroupChannelAdapter,
    WorkgroupChannelAdapter,
)


def _make_wg_cfg(**overrides) -> WorkgroupConfig:
    base = dict(
        name="wg1",
        workspace="/tmp/admin",
        ai_backend="claude-cli",
        model="",
        yolo=False,
        discord_bot_id="bot1",
        admin_discord_category=12345,
        admin_discord_channel=11111,
        discord_peer_channel=0,
    )
    base.update(overrides)
    return WorkgroupConfig(**base)


def _make_sp_cfg(**overrides) -> SpecialistConfig:
    base = dict(
        name="alice",
        ai_backend="claude-cli",
        model="",
        workspace="/tmp/alice",
        display_name="alice",
        discord_channel=0,
    )
    base.update(overrides)
    return SpecialistConfig(**base)


# ─── Null adapter ────────────────────────────────────────────────────────────

def test_null_adapter_protocol_compliance():
    a = NullWorkgroupChannelAdapter()
    assert isinstance(a, WorkgroupChannelAdapter)
    assert a.channel_name == "internal"
    assert a.primary_channel() is None


def test_null_adapter_chat_id_falls_back_to_wg_prefix():
    a = NullWorkgroupChannelAdapter()
    sp = _make_sp_cfg(discord_channel=0)
    assert a.get_specialist_chat_id("alice", sp) == "wg:alice"


def test_null_adapter_methods_are_noops():
    a = NullWorkgroupChannelAdapter()
    sp = _make_sp_cfg()
    wg = _make_wg_cfg()
    router = MagicMock()
    # Should all complete without raising and without touching anything.
    asyncio.run(a.register_admin(router, wg))
    asyncio.run(a.register_peer(router, wg))
    asyncio.run(a.setup_specialist("alice", sp, wg, router))
    out = asyncio.run(a.provision_specialist("alice", sp, wg))
    assert out is sp  # returned unchanged
    asyncio.run(a.cleanup_specialist("alice", sp))
    asyncio.run(a.post_task("alice", sp, "hi", "admin"))
    asyncio.run(a.notify_admin("123", "done"))
    router.handle_message.assert_not_called()


# ─── Discord adapter ─────────────────────────────────────────────────────────

def _make_dc_channel():
    dc = MagicMock()
    dc.register_route = MagicMock()
    dc.register_channel_route = MagicMock()
    dc.send_via_webhook = AsyncMock()
    dc.create_text_channel = AsyncMock(return_value=99999)
    dc.delete_text_channel = AsyncMock()
    dc.send_text = AsyncMock()
    # _ensure_webhook returns an object with .send (or None for fallback)
    wh = MagicMock()
    wh.send = AsyncMock()
    dc._ensure_webhook = AsyncMock(return_value=wh)
    return dc


def test_discord_adapter_protocol_compliance():
    a = DiscordWorkgroupAdapter(dc_channel=_make_dc_channel())
    assert isinstance(a, WorkgroupChannelAdapter)
    assert a.channel_name == "discord"


def test_discord_adapter_primary_channel_is_dc_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    assert a.primary_channel() is dc


def test_discord_adapter_chat_id_uses_specialist_channel_when_set():
    a = DiscordWorkgroupAdapter(dc_channel=_make_dc_channel())
    sp = _make_sp_cfg(discord_channel=42)
    assert a.get_specialist_chat_id("alice", sp) == "42"


def test_discord_adapter_chat_id_falls_back_when_unset():
    a = DiscordWorkgroupAdapter(dc_channel=_make_dc_channel())
    sp = _make_sp_cfg(discord_channel=0)
    assert a.get_specialist_chat_id("alice", sp) == "wg:alice"


def test_discord_adapter_register_admin_does_both_category_and_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    router = MagicMock()
    router._channels = {}
    wg = _make_wg_cfg(admin_discord_category=12345, admin_discord_channel=11111)
    asyncio.run(a.register_admin(router, wg))
    dc.register_route.assert_called_once()
    args = dc.register_route.call_args
    assert args.args[0] is router.handle_message
    assert args.args[1] == [12345]
    dc.register_channel_route.assert_called_once_with(router.handle_message, 11111)
    assert router._channels["discord"] is dc


def test_discord_adapter_register_admin_skips_when_no_category_or_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    router = MagicMock()
    router._channels = {}
    wg = _make_wg_cfg(admin_discord_category=0, admin_discord_channel=0)
    asyncio.run(a.register_admin(router, wg))
    dc.register_route.assert_not_called()
    dc.register_channel_route.assert_not_called()


def test_discord_adapter_setup_specialist_sets_inbound_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    router = MagicMock()
    router._channels = {}
    asyncio.run(a.setup_specialist("alice", _make_sp_cfg(), _make_wg_cfg(), router))
    assert router._channels["discord"] is dc


def test_discord_adapter_provision_specialist_creates_channel_and_mutates_cfg():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=0)
    wg = _make_wg_cfg(admin_discord_category=12345)
    out = asyncio.run(a.provision_specialist("alice", sp, wg))
    dc.create_text_channel.assert_awaited_once_with(12345, "alice")
    assert out is sp
    assert sp.discord_channel == 99999


def test_discord_adapter_provision_specialist_noop_without_category():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=0)
    wg = _make_wg_cfg(admin_discord_category=0)
    asyncio.run(a.provision_specialist("alice", sp, wg))
    dc.create_text_channel.assert_not_awaited()
    assert sp.discord_channel == 0


def test_discord_adapter_cleanup_deletes_channel_when_present():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=42)
    asyncio.run(a.cleanup_specialist("alice", sp))
    dc.delete_text_channel.assert_awaited_once_with(42)


def test_discord_adapter_cleanup_skips_when_no_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=0)
    asyncio.run(a.cleanup_specialist("alice", sp))
    dc.delete_text_channel.assert_not_awaited()


def test_discord_adapter_post_task_uses_webhook():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=42)
    asyncio.run(a.post_task("alice", sp, "do thing", "admin"))
    dc.send_via_webhook.assert_awaited_once_with(42, "admin", "do thing")


def test_discord_adapter_post_task_noop_without_channel():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=0)
    asyncio.run(a.post_task("alice", sp, "do thing", "admin"))
    dc.send_via_webhook.assert_not_awaited()


def test_discord_adapter_notify_admin_uses_webhook_when_available():
    dc = _make_dc_channel()
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    asyncio.run(a.notify_admin("123", "done"))
    dc._ensure_webhook.assert_awaited_once_with("TaskNotification", "123")
    # webhook's send should be called
    wh = dc._ensure_webhook.return_value
    wh.send.assert_awaited_once_with("done", wait=True)


def test_discord_adapter_notify_admin_falls_back_to_send_text():
    dc = _make_dc_channel()
    dc._ensure_webhook = AsyncMock(return_value=None)  # webhook unavailable
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    asyncio.run(a.notify_admin("123", "done"))
    dc.send_text.assert_awaited_once_with("123", "done")


def test_discord_adapter_post_task_swallows_exceptions():
    dc = _make_dc_channel()
    dc.send_via_webhook = AsyncMock(side_effect=RuntimeError("boom"))
    a = DiscordWorkgroupAdapter(dc_channel=dc)
    sp = _make_sp_cfg(discord_channel=42)
    # Should not raise — same as pre-refactor behavior.
    asyncio.run(a.post_task("alice", sp, "do thing", "admin"))
