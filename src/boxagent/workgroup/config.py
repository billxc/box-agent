"""Workgroup config parsing + validation.

The ``WorkgroupConfig`` / ``SpecialistConfig`` dataclasses live in
``boxagent.config`` (passive types read widely across the app), but the
**parsing** of a ``workgroups:`` yaml block is workgroup-specific domain
logic and lives here. ``config.load_config`` imports these lazily, only
when the yaml actually contains workgroups — so deleting the workgroup
module never breaks config loading for a plain (no-workgroup) setup.
"""

from __future__ import annotations

from pathlib import Path

from boxagent.config import (
    ConfigError,
    WorkgroupConfig,
    node_matches,
    resolve_boxagent_dir,
)


def parse_workgroup(
    name: str,
    raw: dict,
    *,
    box_agent_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
) -> WorkgroupConfig:
    """Parse a standalone workgroup configuration block."""
    ba_dir = resolve_boxagent_dir(box_agent_dir)
    config_base = Path(config_dir).expanduser() if config_dir else None

    # Workspace (required)
    workspace = raw.get("workspace", "")
    if workspace:
        ws_path = Path(workspace).expanduser()
        if not ws_path.is_absolute():
            ws_path = ba_dir / ws_path
        workspace = str(ws_path)

    # Discord support has been removed; legacy admin.discord_* / discord_bot_id
    # / transport fields are silently ignored.

    # Agent config
    ai_backend = raw.get("ai_backend", "claude-cli")
    model = raw.get("model", "")
    allowed_users = raw.get("allowed_users", [])
    yolo = bool(raw.get("yolo", False))
    display_name = raw.get("display_name", name)
    display_tool_calls = raw.get("display", {}).get("tool_calls", "silent")

    extra_skill_dirs: list[str] = []
    for raw_dir in raw.get("extra_skill_dirs", []):
        path = Path(raw_dir).expanduser()
        if not path.is_absolute() and config_base is not None:
            path = config_base / path
        extra_skill_dirs.append(str(path))

    heartbeat_interval_seconds = int(raw.get("heartbeat_interval_seconds", 0))
    display_heartbeat = bool(raw.get("display_heartbeat", False))
    # Workgroup admin/specialist always uses the WebChannel; force-enable.
    web_enabled = True

    return WorkgroupConfig(
        name=name,
        workspace=workspace,
        enabled_on_nodes=raw.get("enabled_on_nodes", ""),
        allowed_users=allowed_users,
        model=model,
        ai_backend=ai_backend,
        yolo=yolo,
        display_name=display_name,
        display_tool_calls=display_tool_calls,
        extra_skill_dirs=extra_skill_dirs,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        display_heartbeat=display_heartbeat,
        web_enabled=web_enabled,
    )


def validate_workgroups(
    workgroups: dict[str, WorkgroupConfig],
    node_id: str = "",
) -> None:
    """Validate workgroup configuration."""
    for workgroup_name, workgroup in workgroups.items():
        # Skip workgroups not enabled on this node
        if not node_matches(workgroup.enabled_on_nodes, node_id):
            continue

        if not workgroup.workspace:
            raise ConfigError(f"Workgroup '{workgroup_name}': missing workspace")
