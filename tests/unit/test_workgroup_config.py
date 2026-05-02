"""Unit tests for WorkgroupConfig transport / Discord-mode parsing (yait #4)."""

import logging
from pathlib import Path

import pytest
import yaml

from boxagent.config import (
    ConfigError,
    WorkgroupConfig,
    _parse_workgroup,
    load_config,
)


# ─── is_discord_mode property ────────────────────────────────────────────────

def test_is_discord_mode_explicit_discord():
    wg = WorkgroupConfig(name="x", workspace="/tmp", transport="discord")
    assert wg.is_discord_mode is True


def test_is_discord_mode_explicit_web_overrides_bot_id():
    wg = WorkgroupConfig(
        name="x", workspace="/tmp", transport="web", discord_bot_id="bot1",
    )
    assert wg.is_discord_mode is False


def test_is_discord_mode_auto_from_bot_id():
    wg = WorkgroupConfig(name="x", workspace="/tmp", discord_bot_id="bot1")
    assert wg.is_discord_mode is True


def test_is_discord_mode_default_no_bot_id():
    wg = WorkgroupConfig(name="x", workspace="/tmp")
    assert wg.is_discord_mode is False


# ─── _parse_workgroup transport handling ─────────────────────────────────────

def test_parse_workgroup_transport_web_no_discord_fields(tmp_path):
    raw = {"workspace": str(tmp_path), "transport": "web"}
    wg = _parse_workgroup("wg1", raw)
    assert wg.transport == "web"
    assert wg.is_discord_mode is False
    # transport=web force-enables WebChannel for #3a's adapter
    assert wg.web_enabled is True


def test_parse_workgroup_transport_discord_explicit(tmp_path):
    raw = {
        "workspace": str(tmp_path),
        "transport": "discord",
        # No discord_bot_id needed for parse — validation downstream
    }
    wg = _parse_workgroup("wg1", raw)
    assert wg.transport == "discord"
    assert wg.is_discord_mode is True


def test_parse_workgroup_transport_omitted_auto_discord(tmp_path):
    raw = {
        "workspace": str(tmp_path),
        "discord_bot_id": "bot1",
    }
    wg = _parse_workgroup(
        "wg1", raw, discord_bots={"bot1": "tok"},
    )
    assert wg.transport == ""
    assert wg.is_discord_mode is True


def test_parse_workgroup_transport_omitted_auto_web(tmp_path):
    raw = {"workspace": str(tmp_path)}
    wg = _parse_workgroup("wg1", raw)
    assert wg.transport == ""
    assert wg.is_discord_mode is False


def test_parse_workgroup_transport_web_with_bot_id_warns(tmp_path, caplog):
    raw = {
        "workspace": str(tmp_path),
        "transport": "web",
        "discord_bot_id": "bot1",
    }
    with caplog.at_level(logging.WARNING):
        wg = _parse_workgroup(
            "wg1", raw, discord_bots={"bot1": "tok"},
        )
    assert wg.is_discord_mode is False
    assert any("transport=web" in rec.message for rec in caplog.records)


def test_parse_workgroup_transport_invalid_raises(tmp_path):
    raw = {"workspace": str(tmp_path), "transport": "bogus"}
    with pytest.raises(ConfigError, match="invalid transport"):
        _parse_workgroup("wg1", raw)


def test_parse_workgroup_transport_case_insensitive(tmp_path):
    raw = {"workspace": str(tmp_path), "transport": "WEB"}
    wg = _parse_workgroup("wg1", raw)
    assert wg.transport == "web"


# ─── Backward compat: existing Discord workgroups still load ─────────────────

def test_existing_discord_workgroup_unchanged(tmp_path):
    """A workgroup yaml without `transport` and with discord_bot_id loads
    exactly as before — auto-detected to Discord, web_enabled honors yaml."""
    raw = {
        "workspace": str(tmp_path),
        "discord_bot_id": "bot1",
        "admin": {"discord_category": 12345, "discord_admin_channel": 11111},
        "web_enabled": False,
    }
    wg = _parse_workgroup("wg1", raw, discord_bots={"bot1": "tok"})
    assert wg.transport == ""
    assert wg.is_discord_mode is True
    assert wg.discord_bot_id == "bot1"
    assert wg.admin_discord_category == 12345
    assert wg.admin_discord_channel == 11111
    assert wg.web_enabled is False  # transport != web, so honored as-is
