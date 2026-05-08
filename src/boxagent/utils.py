"""Shared utility functions used across multiple BoxAgent modules.

Pure helpers with no BoxAgent runtime state: dict merging, safe console
output, channel inference, and runtime path resolution.
"""

import logging
import os
import shutil
import sys
from copy import deepcopy
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Dict / IO helpers ──

def deep_merge_dicts(base: dict, override: dict) -> dict:
    """Recursively merge dictionaries; override values win."""
    result = deepcopy(base)
    for key, value in override.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(base_value, value)
        else:
            result[key] = deepcopy(value)
    return result


def safe_print(text: str, *, file=None) -> None:
    """Print text even when the console encoding cannot represent it."""
    stream = file or sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, file=stream)


# ── Channel / chat helpers ──

def infer_platform(chat_id: str) -> str:
    """Best-effort guess for which channel a chat_id originated from."""
    if not chat_id:
        return "unknown"
    if chat_id.startswith("claude-"):
        return "claude"
    if chat_id.startswith("web-"):
        return "web"
    if chat_id.lstrip("-").isdigit():
        return "telegram"
    return "other"


# ── Runtime path helpers ──

_BOX_AGENT_DIR_ENV_NAMES = (
    "BOX_AGENT_DIR",
    "BOXAGENT_DIR",
    "BOX_AGENT_HOME",
    "BOXAGENT_HOME",
)


def resolve_boxagent_dir(box_agent_dir: Path | str | None = None) -> Path:
    """Return the BoxAgent config directory."""
    if box_agent_dir is not None:
        return Path(box_agent_dir).expanduser()
    for env_name in _BOX_AGENT_DIR_ENV_NAMES:
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".boxagent"


def default_config_dir(box_agent_dir: Path | str | None = None) -> Path:
    """Return the default config directory."""
    return resolve_boxagent_dir(box_agent_dir)


def default_local_dir(box_agent_dir: Path | str | None = None) -> Path:
    """Return the default local runtime directory.

    New layout: ``{config_dir}/local/``
    Legacy layout: ``{config_dir}-local/`` (sibling directory)

    If the legacy directory exists and the new one does not, migrate it
    into the new location.
    """
    config_dir = default_config_dir(box_agent_dir)
    new_dir = config_dir / "local"
    legacy_dir = config_dir.with_name(f"{config_dir.name}-local")

    # TODO: Remove legacy migration after 2026-06 when no old installs remain.
    if legacy_dir.is_dir() and not new_dir.exists():
        logger.info(
            "Migrating runtime state: %s → %s", legacy_dir, new_dir,
        )
        shutil.move(str(legacy_dir), str(new_dir))

    return new_dir


def default_workspace_dir(box_agent_dir: Path | str | None = None) -> Path:
    """Return the default workspace directory."""
    return default_config_dir(box_agent_dir) / "workspace"
