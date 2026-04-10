"""Shared path helpers for BoxAgent runtime defaults."""

import os
from pathlib import Path

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

    If the legacy directory exists and the new one does not, print a
    migration hint on first access.
    """
    config_dir = default_config_dir(box_agent_dir)
    new_dir = config_dir / "local"
    legacy_dir = config_dir.with_name(f"{config_dir.name}-local")

    # TODO: Remove legacy migration after 2026-06 when no old installs remain.
    if legacy_dir.is_dir() and not new_dir.exists():
        import shutil
        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "Migrating runtime state: %s → %s", legacy_dir, new_dir,
        )
        shutil.move(str(legacy_dir), str(new_dir))

    return new_dir


def default_workspace_dir(box_agent_dir: Path | str | None = None) -> Path:
    """Return the default workspace directory."""
    return default_config_dir(box_agent_dir) / "workspace"
