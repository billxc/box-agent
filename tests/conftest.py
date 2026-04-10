"""Shared test fixtures and configuration."""

import os
import shutil
import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless explicitly requested."""
    if config.getoption("-m") and "integration" in config.getoption("-m"):
        return
    skip_integration = pytest.mark.skip(reason="use -m integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def has_claude_cli() -> bool:
    """Check if claude CLI is available on PATH."""
    return shutil.which("claude") is not None


def has_telegram_test_config() -> bool:
    """Check if Telegram test env vars are set."""
    return bool(os.environ.get("BOXAGENT_TEST_CHAT_ID"))


requires_claude = pytest.mark.skipif(
    not has_claude_cli(), reason="claude CLI not on PATH"
)
requires_telegram = pytest.mark.skipif(
    not has_telegram_test_config(), reason="BOXAGENT_TEST_CHAT_ID not set"
)
