"""Config loading, validation, and env override."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from copy import deepcopy

import yaml

from boxagent.utils import deep_merge_dicts

from boxagent.utils import default_workspace_dir, resolve_boxagent_dir

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
    # Standalone Telegram push notifier — decoupled from chat bots. Sends a
    # message to `notify_telegram_chat_id` whenever an event matching
    # `notify_telegram_levels` (and optionally `notify_telegram_categories`)
    # is published. Empty token disables.
    notify_telegram_token: str = ""
    notify_telegram_chat_id: str = ""
    notify_telegram_levels: list[str] = field(default_factory=lambda: ["error", "notify"])
    notify_telegram_categories: list[str] = field(default_factory=list)
    # Filesystem path of the JSON-line log file written by main.py's logging
    # FileHandler. Set by main.py after CLI arg / default resolution; read by
    # the Web UI Logs page. None when no file handler is attached.
    log_file: Path | None = None


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

    local_config = _load_local_config(Path(local_dir)) if local_dir else {}
    node_id = str(local_config.get("node_id", "") or "")

    # Compat: fall back to deprecated global.node_id from config.yaml
    base_global_config = raw.get("global", {})
    if not node_id and base_global_config.get("node_id"):
        node_id = str(base_global_config["node_id"])
        logger.warning(
            "global.node_id in config.yaml is deprecated; "
            "move it to local.yaml as node_id"
        )

    if not node_id and local_dir:
        node_id = _ensure_default_node_id(Path(local_dir))

    effective_raw = _apply_node_overrides(raw, node_id)

    global_config = effective_raw.get("global", {})

    # local.yaml global section overrides config.yaml global
    local_global = local_config.get("global", {})
    if isinstance(local_global, dict):
        global_config = {**global_config, **local_global}

    log_level = global_config.get("log_level", "info") or "info"
    log_level = os.environ.get("BOXAGENT_GLOBAL_LOG_LEVEL", log_level) or "info"

    api_port = int(global_config.get("api_port", 0))
    api_port = int(os.environ.get("BOXAGENT_GLOBAL_API_PORT", api_port))

    web_token = str(global_config.get("web_token", "") or "")
    web_token = os.environ.get("BOXAGENT_WEB_TOKEN", web_token)
    web_trust_header = str(
        global_config.get("web_trust_header", "X-BoxAgent-Trusted") or "X-BoxAgent-Trusted"
    )
    web_port = int(global_config.get("web_port", 9292) or 9292)
    web_port = int(os.environ.get("BOXAGENT_WEB_PORT", web_port))
    web_host = str(global_config.get("web_host", "127.0.0.1") or "127.0.0.1")
    web_host = os.environ.get("BOXAGENT_WEB_HOST", web_host)
    # Cluster: shared block in config.yaml describes the topology;
    # `cluster.host` is an ordered fallback list — first-online wins. Single
    # string is accepted for back-compat.
    cluster_config = effective_raw.get("cluster") or {}
    if not isinstance(cluster_config, dict):
        cluster_config = {}
    raw_host = cluster_config.get("host", "") or ""
    if isinstance(raw_host, str):
        host_priority = [raw_host] if raw_host else []
    elif isinstance(raw_host, list):
        host_priority = [str(host).strip() for host in raw_host if str(host).strip()]
    else:
        host_priority = []
    cluster_tunnel_name = str(cluster_config.get("tunnel_name", "") or "boxagent-cluster")
    cluster_token = str(cluster_config.get("token", "") or "")
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

    notify_config = effective_raw.get("notify", {}) or {}
    notify_telegram = (notify_config.get("telegram") or {}) if isinstance(notify_config, dict) else {}
    notify_telegram_token = str(notify_telegram.get("token", "") or "")
    notify_telegram_token = os.environ.get("BOXAGENT_NOTIFY_TELEGRAM_TOKEN", notify_telegram_token)
    notify_telegram_chat_id = str(notify_telegram.get("chat_id", "") or "")
    notify_telegram_chat_id = os.environ.get("BOXAGENT_NOTIFY_TELEGRAM_CHAT_ID", notify_telegram_chat_id)
    raw_levels = notify_telegram.get("levels")
    notify_telegram_levels = (
        [str(level).strip().lower() for level in raw_levels if str(level).strip()]
        if isinstance(raw_levels, list) else ["error", "notify"]
    )
    raw_categories = notify_telegram.get("categories") or []
    notify_telegram_categories = (
        [str(category).strip() for category in raw_categories if str(category).strip()]
        if isinstance(raw_categories, list) else []
    )

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

    # Parse workgroups. Parsing/validation logic lives in the workgroup
    # package; import it lazily so a plain (no-workgroup) config never pulls
    # the workgroup module in.
    workgroups: dict[str, WorkgroupConfig] = {}
    raw_workgroups = effective_raw.get("workgroups", {})
    if raw_workgroups:
        from boxagent.workgroup.config import parse_workgroup, validate_workgroups

        for workgroup_name, workgroup_raw in raw_workgroups.items():
            workgroups[workgroup_name] = parse_workgroup(
                workgroup_name, workgroup_raw,
                box_agent_dir=box_agent_dir, config_dir=config_dir,
            )
        validate_workgroups(workgroups, node_id=node_id)

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
        notify_telegram_token=notify_telegram_token,
        notify_telegram_chat_id=notify_telegram_chat_id,
        notify_telegram_levels=notify_telegram_levels,
        notify_telegram_categories=notify_telegram_categories,
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


def _ensure_default_node_id(local_dir: Path) -> str:
    """Generate a default node_id and persist it to local.yaml.

    Why: without a node_id the gateway becomes an anonymous node — all configs
    that gate on `enabled_on_nodes` get silently skipped (see node_matches),
    and the cluster member can only run as guest. Auto-seeding a stable id on
    first boot avoids the "configured a bunch of bots but none started" trap.
    """
    import re
    import secrets
    import socket

    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / "local.yaml"

    existing: dict = {}
    if local_file.is_file():
        with open(local_file, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if isinstance(loaded, dict):
            existing = loaded
        if existing.get("node_id"):
            return str(existing["node_id"])

    hostname = socket.gethostname().split(".")[0]
    hostname = re.sub(r"[^a-zA-Z0-9-]", "-", hostname).strip("-").lower() or "node"
    node_id = f"{hostname}-{secrets.token_hex(2)}"

    existing["node_id"] = node_id
    with open(local_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False, allow_unicode=True)
    logger.info(
        "No node_id configured; generated default node_id=%s and wrote to %s",
        node_id,
        local_file,
    )
    return node_id


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
    web_config = channels.get("web")
    if web_config is None:
        web_enabled = bool(raw.get("web_enabled", True))
    elif isinstance(web_config, bool):
        web_enabled = web_config
    elif isinstance(web_config, dict):
        web_enabled = bool(web_config.get("enabled", True))
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
    if ai_backend in ("codex-mcp", "codex-acp"):
        raise ConfigError(
            f"Bot '{name}': ai_backend '{ai_backend}' has been removed; use 'claude-cli' or 'codex-cli'"
        )
    if ai_backend not in ("claude-cli", "codex-cli", "agent-sdk-claude", "agent-sdk-copilot"):
        raise ConfigError(f"Bot '{name}': unknown ai_backend '{ai_backend}'")

    extra_skill_dirs: list[str] = []
    config_base = Path(config_dir).expanduser() if config_dir else None
    for raw_dir in raw.get("extra_skill_dirs", []):
        path = Path(raw_dir).expanduser()
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
