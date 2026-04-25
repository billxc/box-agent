"""Config loading, validation, and env override."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from copy import deepcopy

import yaml

from boxagent.utils import deep_merge_dicts

from boxagent.paths import default_workspace_dir

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
    discord_token: str = ""
    discord_allowed_users: list[int] = field(default_factory=list)
    discord_allowed_categories: list[int] = field(default_factory=list)
    model: str = ""
    agent: str = ""
    extra_skill_dirs: list[str] = field(default_factory=list)
    display_tool_calls: str = "summary"
    display_name: str = ""
    enabled_on_nodes: str | list[str] = ""
    yolo: bool = False


@dataclass
class AppConfig:
    node_id: str = ""
    log_level: str = "info"
    api_port: int = 0
    bots: dict[str, BotConfig] = field(default_factory=dict)
    telegram_bots: dict[str, str] = field(default_factory=dict)
    discord_bots: dict[str, str] = field(default_factory=dict)


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

    telegram_bots = _load_telegram_bots(config_dir)
    discord_bots = _load_discord_bots(config_dir)

    bots: dict[str, BotConfig] = {}
    for bot_name, bot_raw in effective_raw.get("bots", {}).items():
        bots[bot_name] = _parse_bot(
            bot_name,
            bot_raw,
            box_agent_dir=box_agent_dir,
            config_dir=config_dir,
            telegram_bots=telegram_bots,
            discord_bots=discord_bots,
        )

    return AppConfig(
        node_id=node_id,
        log_level=log_level,
        api_port=api_port,
        bots=bots,
        telegram_bots=telegram_bots,
        discord_bots=discord_bots,
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


def _load_discord_bots(config_dir: Path) -> dict[str, str]:
    """Load bot name → token mapping from discord_bots.yaml.

    Format:
      bots:
        - id: "my-bot"
          token: "token_string"

    Returns empty dict if file doesn't exist.
    """
    bots_file = config_dir / "discord_bots.yaml"
    if not bots_file.is_file():
        return {}

    with open(bots_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return {}

    entries = raw.get("bots")
    if not isinstance(entries, list):
        return {}

    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token", "")).strip()
        if not token:
            continue
        name = entry.get("id") or entry.get("name")
        if name:
            result[str(name)] = token
    return result


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
    discord_bots: dict[str, str] | None = None,
) -> BotConfig:
    channels = raw.get("channels", {})

    # --- Telegram channel (optional) ---
    telegram = channels.get("telegram", {})
    telegram_token = ""
    telegram_allowed_users: list[int] = []
    if telegram:
        telegram_token = telegram.get("token")
        if not telegram_token:
            bot_id = telegram.get("bot_id")
            if bot_id and telegram_bots:
                telegram_token = telegram_bots.get(str(bot_id))
                if not telegram_token:
                    raise ConfigError(
                        f"Bot '{name}': bot_id '{bot_id}' not found in telegram_bots.yaml"
                    )
            elif bot_id:
                raise ConfigError(
                    f"Bot '{name}': bot_id '{bot_id}' specified but telegram_bots.yaml not found"
                )
        telegram_allowed_users = telegram.get("allowed_users", [])

    # --- Discord channel (optional) ---
    discord = channels.get("discord", {})
    discord_token = ""
    discord_allowed_users: list[int] = []
    discord_allowed_categories: list[int] = []
    if discord:
        discord_token = discord.get("token", "")
        if not discord_token:
            bot_id = discord.get("bot_id")
            if bot_id and discord_bots:
                discord_token = discord_bots.get(str(bot_id))
                if not discord_token:
                    raise ConfigError(
                        f"Bot '{name}': bot_id '{bot_id}' not found in discord_bots.yaml"
                    )
            elif bot_id:
                raise ConfigError(
                    f"Bot '{name}': bot_id '{bot_id}' specified but discord_bots.yaml not found"
                )
            else:
                raise ConfigError(f"Bot '{name}': missing channels.discord.token or bot_id")
        discord_allowed_users = discord.get("allowed_users", [])
        discord_allowed_categories = discord.get("allowed_categories", [])

    # At least one channel must be configured
    if not telegram_token and not discord_token:
        raise ConfigError(
            f"Bot '{name}': at least one channel (telegram or discord) must be configured"
        )

    # Union of all allowed users for Router auth
    allowed_users = list(set(telegram_allowed_users + discord_allowed_users))

    default_workspace = str(default_workspace_dir(box_agent_dir))
    workspace = raw.get("workspace") or default_workspace
    workspace = str(Path(workspace).expanduser())
    display = raw.get("display", {})

    env_prefix = f"BOXAGENT_{name.upper().replace('-', '_')}_"
    workspace = os.environ.get(f"{env_prefix}workspace", workspace) or default_workspace
    workspace = str(Path(workspace).expanduser())

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
        discord_token=discord_token,
        discord_allowed_users=discord_allowed_users,
        discord_allowed_categories=discord_allowed_categories,
        model=raw.get("model", ""),
        agent=raw.get("agent", ""),
        extra_skill_dirs=extra_skill_dirs,
        display_tool_calls=display.get("tool_calls", "summary"),
        display_name=raw.get("display_name", ""),
        enabled_on_nodes=raw.get("enabled_on_nodes", ""),
        yolo=bool(raw.get("yolo", False)),
    )
