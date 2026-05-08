"""Config loading, validation, and env override."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from copy import deepcopy

import yaml

from boxagent.utils import deep_merge_dicts

from boxagent.paths import default_workspace_dir, resolve_boxagent_dir

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when config validation fails."""


@dataclass
class BotConfig:
    name: str
    ai_backend: str
    workspace: str
    telegram_token: str = ""
    allowed_users: list[int] = field(default_factory=list)
    telegram_allowed_users: list[int] = field(default_factory=list)
    model: str = ""
    agent: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    display_tool_calls: str = "summary"
    display_name: str = ""
    enabled_on_nodes: str | list[str] = ""
    yolo: bool = False
    web_enabled: bool = True
    passthrough: bool = False  # raw bot: skip all BoxAgent context/MCP injection


@dataclass
class SpecialistConfig:
    """A specialist agent within a workgroup."""

    name: str
    model: str = ""
    workspace: str = ""
    ai_backend: str = ""
    display_name: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    template: str = ""  # template name used at create time (metadata only)


@dataclass
class WorkgroupConfig:
    """A standalone workgroup: admin agent + N specialist agents.

    The admin is created inline (not referencing the bots section).
    Specialists are virtual agents reachable only via send_to_agent.
    """

    name: str
    workspace: str = ""             # root directory; admin uses {workspace}/.boxagent-workgroup/admin/
    enabled_on_nodes: str | list[str] = ""  # empty = run everywhere
    # Agent config
    allowed_users: list[int] = field(default_factory=list)
    model: str = ""
    ai_backend: str = "claude-cli"
    yolo: bool = False
    display_name: str = ""
    display_tool_calls: str = "silent"
    extra_skill_dirs: list[str] = field(default_factory=list)
    heartbeat_interval_seconds: int = 0  # 0 = disabled
    display_heartbeat: bool = False
    web_enabled: bool = True
    specialists: dict[str, SpecialistConfig] = field(default_factory=dict)

    @property
    def workgroup_dir(self) -> str:
        """The .boxagent-workgroup directory under workspace."""
        return str(Path(self.workspace) / ".boxagent-workgroup") if self.workspace else ""

    @property
    def admin_workspace(self) -> str:
        return str(Path(self.workgroup_dir) / "admin") if self.workgroup_dir else ""

    def specialist_workspace(self, specialist_name: str) -> str:
        if not self.workgroup_dir:
            return ""
        return str(Path(self.workgroup_dir) / "specialists" / specialist_name)


@dataclass
class AppConfig:
    node_id: str = ""
    log_level: str = "info"
    api_port: int = 0
    mcp_port: int = 0  # MCP HTTP server port (0 = auto-assign)
    web_port: int = 9292  # Web UI port (configurable; separate from api_port)
    web_host: str = "127.0.0.1"  # Bind address for the web UI; "0.0.0.0" for LAN/phone
    web_token: str = ""
    web_trust_header: str = "X-BoxAgent-Trusted"
    machine_id: str = ""
    # Hub-and-spoke clustering (optional):
    # `host_priority` is the ordered fallback list of candidate hosts. Whoever
    # comes first in the list and is reachable becomes the active host; lower-
    # priority candidates run as guests. The active role is decided at runtime
    # by HostElection — not by this config alone.
    host_priority: list[str] = field(default_factory=list)
    # Index of this node in host_priority, or -1 if not a candidate at all.
    my_host_index: int = -1
    # Devtunnel name shared by every candidate (only one node at a time hosts it).
    cluster_tunnel: str = ""
    # Cluster shared secret (used both as guest_token by hosts and as host_token
    # by guests). Identical content; the role-aware split is a leftover from
    # when the role was static at parse time.
    guest_token: str = ""
    host_token: str = ""
    bots: dict[str, BotConfig] = field(default_factory=dict)
    workgroups: dict[str, WorkgroupConfig] = field(default_factory=dict)
    telegram_bots: dict[str, str] = field(default_factory=dict)


def _validate_workgroups(
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


def load_config(
    config_dir: Path | str,
    box_agent_dir: Path | str | None = None,
    local_dir: Path | str | None = None,
) -> AppConfig:
    """Load and validate config.yaml."""
    config_dir = Path(config_dir)
    config_file = config_dir / "config.yaml"

    if not config_file.exists():
        raise ConfigError(f"Config file not found: {config_file}")

    with open(config_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ConfigError("Config file is empty")

    local_cfg = _load_local_config(Path(local_dir)) if local_dir else {}
    node_id = str(local_cfg.get("node_id", "") or "")

    # Compat: fall back to deprecated global.node_id from config.yaml
    base_global_cfg = raw.get("global", {})
    if not node_id and base_global_cfg.get("node_id"):
        node_id = str(base_global_cfg["node_id"])
        logger.warning(
            "global.node_id in config.yaml is deprecated; "
            "move it to local.yaml as node_id"
        )

    effective_raw = _apply_node_overrides(raw, node_id)

    global_cfg = effective_raw.get("global", {})

    # local.yaml global section overrides config.yaml global
    local_global = local_cfg.get("global", {})
    if isinstance(local_global, dict):
        global_cfg = {**global_cfg, **local_global}

    log_level = global_cfg.get("log_level", "info")
    log_level = os.environ.get("BOXAGENT_GLOBAL_LOG_LEVEL", log_level)

    api_port = int(global_cfg.get("api_port", 0))
    api_port = int(os.environ.get("BOXAGENT_GLOBAL_API_PORT", api_port))

    web_token = str(global_cfg.get("web_token", "") or "")
    web_token = os.environ.get("BOXAGENT_WEB_TOKEN", web_token)
    web_trust_header = str(
        global_cfg.get("web_trust_header", "X-BoxAgent-Trusted") or "X-BoxAgent-Trusted"
    )
    web_port = int(global_cfg.get("web_port", 9292) or 9292)
    web_port = int(os.environ.get("BOXAGENT_WEB_PORT", web_port))
    web_host = str(global_cfg.get("web_host", "127.0.0.1") or "127.0.0.1")
    web_host = os.environ.get("BOXAGENT_WEB_HOST", web_host)
    # Cluster: shared block in config.yaml describes the topology;
    # `cluster.host` is an ordered fallback list — first-online wins. Single
    # string is accepted for back-compat.
    cluster_cfg = effective_raw.get("cluster") or {}
    if not isinstance(cluster_cfg, dict):
        cluster_cfg = {}
    raw_host = cluster_cfg.get("host", "") or ""
    if isinstance(raw_host, str):
        host_priority = [raw_host] if raw_host else []
    elif isinstance(raw_host, list):
        host_priority = [str(h).strip() for h in raw_host if str(h).strip()]
    else:
        host_priority = []
    cluster_tunnel_name = str(cluster_cfg.get("tunnel_name", "") or "boxagent-cluster")
    cluster_token = str(cluster_cfg.get("token", "") or "")
    cluster_token = os.environ.get("BOXAGENT_CLUSTER_TOKEN", cluster_token)
    machine_id = node_id or (host_priority[0] if host_priority else "")
    my_host_index = host_priority.index(node_id) if (node_id and node_id in host_priority) else -1
    # Every candidate (anyone in host_priority) carries both tokens — the active
    # role is decided at runtime by HostElection. Non-candidates with
    # cluster.host configured still get host_token so they can dial in as
    # permanent guests.
    is_candidate = my_host_index >= 0
    has_cluster = bool(host_priority)
    guest_token = cluster_token if is_candidate else ""
    host_token = cluster_token if has_cluster else ""
    cluster_tunnel = cluster_tunnel_name if has_cluster else ""

    telegram_bots = _load_telegram_bots(config_dir)

    bots: dict[str, BotConfig] = {}
    for bot_name, bot_raw in effective_raw.get("bots", {}).items():
        # Skip bots not enabled on this node (avoids validating placeholder bot_ids)
        bot_nodes = bot_raw.get("enabled_on_nodes", "")
        if node_id and bot_nodes and not node_matches(bot_nodes, node_id):
            logger.debug("Bot '%s' skipped during config load (enabled_on_nodes=%s, current=%s)", bot_name, bot_nodes, node_id)
            continue
        bots[bot_name] = _parse_bot(
            bot_name,
            bot_raw,
            box_agent_dir=box_agent_dir,
            config_dir=config_dir,
            telegram_bots=telegram_bots,
        )

    # Parse workgroups
    workgroups: dict[str, WorkgroupConfig] = {}
    for workgroup_name, workgroup_raw in effective_raw.get("workgroups", {}).items():
        workgroups[workgroup_name] = _parse_workgroup(
            workgroup_name, workgroup_raw,
            box_agent_dir=box_agent_dir, config_dir=config_dir,
        )

    _validate_workgroups(workgroups, node_id=node_id)

    return AppConfig(
        node_id=node_id,
        log_level=log_level,
        api_port=api_port,
        web_token=web_token,
        web_trust_header=web_trust_header,
        web_port=web_port,
        web_host=web_host,
        machine_id=machine_id,
        host_priority=host_priority,
        my_host_index=my_host_index,
        guest_token=guest_token,
        cluster_tunnel=cluster_tunnel,
        host_token=host_token,
        bots=bots,
        workgroups=workgroups,
        telegram_bots=telegram_bots,
    )


def _apply_node_overrides(raw: dict, node_id: str) -> dict:
    """Apply node-specific overrides from config.yaml.

    Shape:
      node_overrides:
        <node_id>:
          global: {...}
          bots: {...}
    """
    overrides = raw.get("node_overrides")
    if overrides is None:
        return raw

    if not isinstance(overrides, dict):
        raise ConfigError("node_overrides must be a mapping")

    base = deepcopy(raw)
    base.pop("node_overrides", None)

    if not node_id:
        return base

    node_override = overrides.get(node_id)
    if node_override is None:
        return base

    if not isinstance(node_override, dict):
        raise ConfigError(
            f"node_overrides.{node_id} must be a mapping"
        )

    return deep_merge_dicts(base, node_override)


def _load_telegram_bots(config_dir: Path) -> dict[str, str]:
    """Load bot_id/bot_name → token mapping from telegram_bots.yaml.

    Supports two formats:
      1. Flat dict:  {"bot_id": "bot_id:token", ...}
      2. List:       bots: [{id: "name", token: "bot_id:token"}, ...]

    Returns empty dict if file doesn't exist.
    """
    bots_file = config_dir / "telegram_bots.yaml"
    if not bots_file.is_file():
        return {}

    with open(bots_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw:
        return {}

    # List format: bots: [{id: ..., token: ...}, ...]
    if isinstance(raw, dict) and "bots" in raw and isinstance(raw["bots"], list):
        result = {}
        for entry in raw["bots"]:
            if not isinstance(entry, dict):
                continue
            token = str(entry.get("token", "")).strip()
            if not token:
                continue
            # Key by bot_id (numeric part before colon) and by name
            bot_id = token.split(":")[0]
            result[bot_id] = token
            name = entry.get("id") or entry.get("name")
            if name:
                result[str(name)] = token
        return result

    # Flat dict format: {"bot_id": "full_token", ...}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}

    return {}


def _load_local_config(local_dir: Path) -> dict:
    """Load local.yaml from the local runtime directory.

    Returns the parsed dict, or empty dict if file doesn't exist.
    """
    local_file = local_dir / "local.yaml"
    if not local_file.is_file():
        return {}

    with open(local_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return {}

    return raw


def node_matches(enabled_on: str | list[str], node_id: str) -> bool:
    """Check if node_id matches the enabled_on_nodes filter.

    Returns True (= run everywhere) when enabled_on is empty/unset.
    """
    if not enabled_on:
        return True
    if isinstance(enabled_on, list):
        return node_id in enabled_on
    return enabled_on == node_id


def _parse_bot(
    name: str,
    raw: dict,
    box_agent_dir: Path | str | None = None,
    config_dir: Path | str | None = None,
    telegram_bots: dict[str, str] | None = None,
) -> BotConfig:
    channels = raw.get("channels", {})

    # --- Telegram channel (optional — web is always available as ingress) ---
    telegram = channels.get("telegram", {})
    telegram_token = ""
    telegram_allowed_users: list[int] = []
    if telegram:
        telegram_token = telegram.get("token", "") or ""
        if not telegram_token:
            bot_id = telegram.get("bot_id")
            if bot_id and telegram_bots:
                telegram_token = telegram_bots.get(str(bot_id), "") or ""
                if not telegram_token:
                    raise ConfigError(
                        f"Bot '{name}': bot_id '{bot_id}' not found in telegram_bots.yaml"
                    )
            elif bot_id:
                raise ConfigError(
                    f"Bot '{name}': bot_id '{bot_id}' specified but telegram_bots.yaml not found"
                )
            # else: no token + no bot_id → telegram is just not configured;
            # bot will run on the web channel only.
        telegram_allowed_users = telegram.get("allowed_users", [])

    # Discord support has been removed; legacy channels.discord blocks in
    # config.yaml are silently ignored.

    # --- Web channel (default on, additive — opt out with channels.web: false) ---
    web_cfg = channels.get("web")
    if web_cfg is None:
        web_enabled = bool(raw.get("web_enabled", True))
    elif isinstance(web_cfg, bool):
        web_enabled = web_cfg
    elif isinstance(web_cfg, dict):
        web_enabled = bool(web_cfg.get("enabled", True))
    else:
        web_enabled = True

    # No "at least one channel" check — web is always reachable in-process even
    # if web_enabled is false (the bot can still be driven via API/scheduler),
    # and telegram is purely optional.

    allowed_users = list(set(telegram_allowed_users))

    ba_dir = resolve_boxagent_dir(box_agent_dir)
    default_workspace = str(default_workspace_dir(box_agent_dir))
    workspace = raw.get("workspace") or default_workspace
    ws_path = Path(workspace).expanduser()
    if not ws_path.is_absolute():
        ws_path = ba_dir / ws_path
    workspace = str(ws_path)
    display = raw.get("display", {})

    env_prefix = f"BOXAGENT_{name.upper().replace('-', '_')}_"
    workspace = os.environ.get(f"{env_prefix}workspace", workspace) or default_workspace
    ws_path = Path(workspace).expanduser()
    if not ws_path.is_absolute():
        ws_path = ba_dir / ws_path
    workspace = str(ws_path)

    ai_backend = raw.get("ai_backend", "claude-cli")
    if ai_backend == "codex-mcp":
        raise ConfigError(
            f"Bot '{name}': ai_backend 'codex-mcp' has been deprecated and removed; use 'codex-acp' instead"
        )
    if ai_backend not in ("claude-cli", "codex-cli", "codex-acp"):
        raise ConfigError(f"Bot '{name}': unknown ai_backend '{ai_backend}'")

    extra_skill_dirs: list[str] = []
    config_base = Path(config_dir).expanduser() if config_dir else None
    for d in raw.get("extra_skill_dirs", []):
        path = Path(d).expanduser()
        if not path.is_absolute() and config_base is not None:
            path = config_base / path
        extra_skill_dirs.append(str(path))

    return BotConfig(
        name=name,
        ai_backend=ai_backend,
        workspace=workspace,
        telegram_token=telegram_token,
        allowed_users=allowed_users,
        telegram_allowed_users=telegram_allowed_users,
        model=raw.get("model", ""),
        agent=raw.get("agent", ""),
        extra_skill_dirs=extra_skill_dirs,
        display_tool_calls=display.get("tool_calls", "summary"),
        display_name=raw.get("display_name", ""),
        enabled_on_nodes=raw.get("enabled_on_nodes", ""),
        yolo=bool(raw.get("yolo", False)),
        web_enabled=web_enabled,
    )


def _parse_workgroup(
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
    for d in raw.get("extra_skill_dirs", []):
        path = Path(d).expanduser()
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
