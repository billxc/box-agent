"""Unit tests for workgroup channel adapters (Web + Null)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from boxagent.config import SpecialistConfig, WorkgroupConfig
from boxagent.workgroup.channel_adapter import (
    NullWorkgroupChannelAdapter,
    WebWorkgroupAdapter,
    WorkgroupChannelAdapter,
)


def _make_wg_cfg(**overrides) -> WorkgroupConfig:
    base = dict(name="wg1", workspace="/tmp/admin")
    base.update(overrides)
    return WorkgroupConfig(**base)


def _make_sp_cfg(**overrides) -> SpecialistConfig:
    base = dict(
        name="alice",
        ai_backend="claude-cli",
        model="",
        workspace="/tmp/alice",
        display_name="alice",
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
    assert a.get_specialist_chat_id("alice", _make_sp_cfg()) == "wg:alice"


def test_null_adapter_methods_are_noops():
    a = NullWorkgroupChannelAdapter()
    sp = _make_sp_cfg()
    wg = _make_wg_cfg()
    router = MagicMock()
    asyncio.run(a.setup_specialist("alice", sp, wg, router))
    out = asyncio.run(a.provision_specialist("alice", sp, wg))
    assert out is sp
    asyncio.run(a.cleanup_specialist("alice", sp))
    asyncio.run(a.post_task("alice", sp, "hi", "admin"))
    asyncio.run(a.notify_admin("123", "done"))
    router.handle_message.assert_not_called()


# ─── Web adapter ─────────────────────────────────────────────────────────────

def _make_web_channel():
    wc = MagicMock()
    wc._publish = MagicMock()
    wc.send_text = AsyncMock()
    wc._allocate_id = MagicMock(return_value="id-1")
    return wc


def test_web_adapter_protocol_compliance():
    a = WebWorkgroupAdapter(web_channel=_make_web_channel())
    assert isinstance(a, WorkgroupChannelAdapter)
    assert a.channel_name == "web"


def test_web_adapter_primary_channel_is_web_channel():
    wc = _make_web_channel()
    a = WebWorkgroupAdapter(web_channel=wc)
    assert a.primary_channel() is wc


def test_web_adapter_chat_id_is_virtual_wg_prefix():
    a = WebWorkgroupAdapter(web_channel=_make_web_channel())
    sp = _make_sp_cfg(discord_channel=999)  # ignored
    assert a.get_specialist_chat_id("alice", sp) == "wg:alice"


def test_web_adapter_setup_specialist_wires_inbound_channel():
    wc = _make_web_channel()
    a = WebWorkgroupAdapter(web_channel=wc)
    router = MagicMock()
    router._channels = {}
    asyncio.run(a.setup_specialist("alice", _make_sp_cfg(), _make_wg_cfg(), router))
    assert router._channels["web"] is wc


def test_web_adapter_provision_returns_sp_cfg_unchanged():
    a = WebWorkgroupAdapter(web_channel=_make_web_channel())
    sp = _make_sp_cfg()
    out = asyncio.run(a.provision_specialist("alice", sp, _make_wg_cfg()))
    assert out is sp


def test_web_adapter_cleanup_specialist_is_noop():
    wc = _make_web_channel()
    a = WebWorkgroupAdapter(web_channel=wc)
    asyncio.run(a.cleanup_specialist("alice", _make_sp_cfg()))
    wc._publish.assert_not_called()


def test_web_adapter_post_task_publishes_user_message_to_specialist_chat():
    wc = _make_web_channel()
    a = WebWorkgroupAdapter(web_channel=wc)
    asyncio.run(a.post_task("alice", _make_sp_cfg(), "do thing", "admin"))
    wc._publish.assert_called_once()
    chat_id, event = wc._publish.call_args.args
    assert chat_id == "wg:alice"
    assert event["type"] == "message"
    assert event["role"] == "user"
    assert event["text"] == "[admin] do thing"
    assert event["message_id"] == "id-1"


def test_web_adapter_notify_admin_uses_send_text():
    wc = _make_web_channel()
    a = WebWorkgroupAdapter(web_channel=wc)
    asyncio.run(a.notify_admin("123", "done"))
    wc.send_text.assert_awaited_once_with("123", "done")
