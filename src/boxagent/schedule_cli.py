"""CLI subcommands for managing schedules in a single YAML file."""

import json
import sys
from pathlib import Path

from boxagent.utils import safe_print as _safe_print

import yaml
from croniter import croniter
from boxagent.paths import default_config_dir, default_local_dir
from boxagent.scheduler import SCHEDULE_NODE_OVERRIDES_KEY, load_schedule_entries


class _ScheduleDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ScheduleDumper.add_representer(str, _represent_str)


API_PORT_FILE = "api-port.txt"


def build_schedule_parser(subparsers) -> None:
    """Register 'schedule' subcommand with sub-subparsers."""
    schedule = subparsers.add_parser("schedule", help="Manage scheduled tasks")
    schedule_sub = schedule.add_subparsers(dest="schedule_cmd")

    # add
    add = schedule_sub.add_parser("add", help="Create a new schedule")
    add.add_argument("--id", required=True, help="Unique task ID")
    add.add_argument("--cron", required=True, help="Cron expression (5-field)")
    add.add_argument("--prompt", required=True, help="Prompt to send")
    add.add_argument("--mode", default="isolate", choices=["isolate", "append"],
                     help="Execution mode (default: isolate)")
    add.add_argument("--bot", default="", help="Bot name (required for append mode)")
    add.add_argument("--ai-backend", default="", choices=["", "claude-cli", "codex-cli", "codex-acp"], help="Backend override (required for isolate mode)")
    add.add_argument("--model", default="", help="Model override (required for isolate mode)")
    add.add_argument("--enabled-on-nodes", default="", help="Only run on this node")
    add.add_argument("--enabled", default=True, type=_parse_bool,
                     help="Enable the schedule (default: true)")
    add.set_defaults(func=schedule_add)

    # list
    ls = schedule_sub.add_parser("list", help="List all schedules")
    ls.set_defaults(func=schedule_list)

    # del
    dl = schedule_sub.add_parser("del", help="Delete a schedule")
    dl.add_argument("--id", required=True, help="Task ID to delete")
    dl.set_defaults(func=schedule_del)

    # enable
    en = schedule_sub.add_parser("enable", help="Enable a schedule")
    en.add_argument("--id", required=True, help="Task ID")
    en.set_defaults(func=schedule_enable)

    # disable
    dis = schedule_sub.add_parser("disable", help="Disable a schedule")
    dis.add_argument("--id", required=True, help="Task ID")
    dis.set_defaults(func=schedule_disable)

    # show
    show = schedule_sub.add_parser("show", help="Show a schedule's details")
    show.add_argument("--id", required=True, help="Task ID")
    show.set_defaults(func=schedule_show)

    # run
    run = schedule_sub.add_parser("run", help="Run a schedule once immediately")
    run.add_argument("--id", required=True, help="Task ID to run")
    run.add_argument("--sync", action="store_true", default=False,
                     help="Wait for completion and print output (default: async fire-and-forget)")
    run.set_defaults(func=schedule_run)

    # logs
    logs = schedule_sub.add_parser("logs", help="Show schedule execution logs")
    logs.add_argument("--id", default="", help="Filter by task ID")
    logs.add_argument("-n", "--lines", type=int, default=20, help="Number of recent entries (default: 20)")
    logs.add_argument("--json", dest="output_json", action="store_true", default=False,
                      help="Output as JSON")
    logs.set_defaults(func=schedule_logs)


def _parse_bool(v: str) -> bool:
    return v.lower() in ("true", "1", "yes")


def _schedules_file(args) -> Path:
    box_agent_dir = getattr(args, "box_agent_dir", None)
    config_dir = getattr(args, "config", None) or default_config_dir(box_agent_dir)
    return Path(config_dir) / "schedules.yaml"


def _load_all(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _load_node_id(args) -> str:
    """Read node_id from local/local.yaml, default empty string."""
    local_file = default_local_dir(getattr(args, "box_agent_dir", None)) / "local.yaml"
    if not local_file.is_file():
        return ""
    with open(local_file, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("node_id", "") or "")


def _load_effective_schedules(args) -> dict[str, dict]:
    """Load schedules visible to the current node after node_overrides merge."""
    return load_schedule_entries(_schedules_file(args), node_id=_load_node_id(args))


def _save_all(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, Dumper=_ScheduleDumper, default_flow_style=False, sort_keys=False, allow_unicode=False)


def _summarize_prompt(prompt: str, limit: int = 40) -> str:
    summary = " ".join(str(prompt).split())
    if len(summary) > limit:
        return summary[: limit - 3] + "..."
    return summary


def schedule_add(args) -> None:
    """Add a new schedule entry."""
    if args.id == SCHEDULE_NODE_OVERRIDES_KEY:
        print(
            f"Error: '{SCHEDULE_NODE_OVERRIDES_KEY}' is a reserved schedule id",
            file=sys.stderr,
        )
        sys.exit(1)

    if not croniter.is_valid(args.cron):
        print(f"Error: invalid cron expression '{args.cron}'", file=sys.stderr)
        sys.exit(1)

    if args.mode == "append" and not args.bot:
        print("Error: --bot is required when mode=append", file=sys.stderr)
        sys.exit(1)
    if args.mode == "isolate" and not args.ai_backend:
        print("Error: --ai-backend is required when mode=isolate", file=sys.stderr)
        sys.exit(1)
    if args.mode == "isolate" and not args.model:
        print("Error: --model is required when mode=isolate", file=sys.stderr)
        sys.exit(1)

    path = _schedules_file(args)
    all_scheds = _load_all(path)

    if args.id in all_scheds:
        print(f"Error: schedule '{args.id}' already exists", file=sys.stderr)
        sys.exit(1)

    entry = {
        "cron": args.cron,
        "prompt": args.prompt,
        "mode": args.mode,
        "bot": args.bot,
        "ai_backend": args.ai_backend,
        "model": args.model,
        "enabled_on_nodes": args.enabled_on_nodes,
        "enabled": args.enabled,
    }
    all_scheds[args.id] = entry
    _save_all(path, all_scheds)
    print(f"Created schedule '{args.id}'")


def schedule_list(args) -> None:
    """List all schedules."""
    all_scheds = _load_effective_schedules(args)

    if not all_scheds:
        print("No schedules found.")
        return

    print(f"{'ID':<20} {'CRON':<16} {'MODE':<8} {'ENABLED':<8} {'PROMPT'}")
    print("-" * 80)
    for task_id, entry in all_scheds.items():
        cron = entry.get("cron", "?")
        mode = entry.get("mode", "isolate")
        enabled = "yes" if entry.get("enabled", True) else "no"
        prompt = _summarize_prompt(entry.get("prompt", ""))
        print(f"{task_id:<20} {cron:<16} {mode:<8} {enabled:<8} {prompt}")


def schedule_del(args) -> None:
    """Delete a schedule."""
    path = _schedules_file(args)
    all_scheds = _load_all(path)

    if args.id not in all_scheds:
        print(f"Error: schedule '{args.id}' not found", file=sys.stderr)
        sys.exit(1)

    del all_scheds[args.id]
    _save_all(path, all_scheds)
    print(f"Deleted schedule '{args.id}'")


def schedule_enable(args) -> None:
    """Enable a schedule."""
    _set_enabled(args, True)


def schedule_disable(args) -> None:
    """Disable a schedule."""
    _set_enabled(args, False)


def _set_enabled(args, enabled: bool) -> None:
    path = _schedules_file(args)
    all_scheds = _load_all(path)

    if args.id not in all_scheds:
        print(f"Error: schedule '{args.id}' not found", file=sys.stderr)
        sys.exit(1)

    all_scheds[args.id]["enabled"] = enabled
    _save_all(path, all_scheds)
    state = "enabled" if enabled else "disabled"
    print(f"Schedule '{args.id}' {state}")


def schedule_show(args) -> None:
    """Show a schedule's details."""
    all_scheds = _load_effective_schedules(args)

    if args.id not in all_scheds:
        print(f"Error: schedule '{args.id}' not found", file=sys.stderr)
        sys.exit(1)

    entry = {args.id: all_scheds[args.id]}
    print(yaml.dump(entry, Dumper=_ScheduleDumper, default_flow_style=False, sort_keys=False, allow_unicode=False), end="")


def schedule_run(args) -> None:
    """Run a schedule once immediately via the gateway HTTP API."""
    import httpx

    sync = getattr(args, "sync", False)
    sock_path = _get_sock_path(args)
    api_ports = _get_api_ports(args)

    payload = {"id": args.id}
    if not sync:
        payload["async"] = True

    timeout = 10.0 if not sync else 300.0

    connected = False
    for transport, url in _request_targets(sock_path, api_ports):
        try:
            with httpx.Client(transport=transport, timeout=timeout) as client:
                resp = client.post(
                    url,
                    json=payload,
                )
            connected = True
            data = resp.json()
            if data.get("ok"):
                if not sync:
                    _safe_print(f"Schedule '{args.id}' triggered (async)")
                else:
                    output = data.get("output", "")
                    if output:
                        _safe_print(output)
                    else:
                        _safe_print(f"Schedule '{args.id}' completed (no output)")
            else:
                print(f"Error: {data.get('error', 'unknown')}", file=sys.stderr)
                sys.exit(1)
            break
        except httpx.ConnectError:
            continue

    if not connected:
        print("Error: gateway not running", file=sys.stderr)
        sys.exit(1)


def _get_sock_path(args) -> Path:
    """Return the Unix socket path."""
    return default_local_dir(getattr(args, "box_agent_dir", None)) / "api.sock"


def _get_api_port_file(args) -> Path:
    """Return the runtime API port file path."""
    return default_local_dir(getattr(args, "box_agent_dir", None)) / API_PORT_FILE


def _request_targets(sock_path: Path, api_ports: list[int]):
    """Yield request targets to try: Unix socket first, then TCP ports."""
    import httpx

    if sock_path.exists():
        yield httpx.HTTPTransport(uds=str(sock_path)), "http://localhost/api/schedule/run"
    for port in api_ports:
        yield httpx.HTTPTransport(), f"http://127.0.0.1:{port}/api/schedule/run"


def _get_api_ports(args) -> list[int]:
    """Return candidate TCP ports to try, preferring runtime-discovered ports."""
    ports: list[int] = []
    for port in (_get_runtime_api_port(args), _get_api_port(args)):
        if port and port not in ports:
            ports.append(port)
    return ports


def _get_runtime_api_port(args) -> int:
    """Read the runtime-discovered API port, default 0 when absent/invalid."""
    port_file = _get_api_port_file(args)
    if not port_file.is_file():
        return 0
    try:
        return int(port_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _get_api_port(args) -> int:
    """Read api_port from config.yaml, default 0 (disabled)."""
    box_agent_dir = getattr(args, "box_agent_dir", None)
    config_dir = getattr(args, "config", None) or default_config_dir(box_agent_dir)
    config_file = Path(config_dir) / "config.yaml"
    if config_file.is_file():
        with open(config_file, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return int(config.get("global", {}).get("api_port", 0))
    return 0


def _local_dir(args) -> Path:
    return default_local_dir(getattr(args, "box_agent_dir", None))


def _load_run_logs(local_dir: Path, task_id: str = "") -> list[dict]:
    """Load schedule run log entries from jsonl files.

    Returns a list of records sorted by time descending.
    """
    runs_dir = local_dir / "schedule-runs"
    if not runs_dir.is_dir():
        return []

    entries: list[dict] = []
    if task_id:
        files = [runs_dir / f"{task_id}.jsonl"]
    else:
        files = list(runs_dir.glob("*.jsonl"))

    for f in files:
        if not f.is_file():
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    entries.sort(key=lambda e: e.get("time", ""), reverse=True)
    return entries


def schedule_logs(args) -> None:
    """Show schedule execution logs."""
    task_id = getattr(args, "id", "")
    n = getattr(args, "lines", 20)
    entries = _load_run_logs(_local_dir(args), task_id=task_id)

    if not entries:
        if task_id:
            print(f"No logs found for '{task_id}'.")
        else:
            print("No schedule logs found.")
        return

    entries = entries[:n]

    if getattr(args, "output_json", False):
        _safe_print(json.dumps(entries, indent=2, ensure_ascii=False))
        return

    for e in entries:
        time = e.get("time", "?")
        tid = e.get("task", "?")
        mode = e.get("mode", "?")
        backend = e.get("ai_backend", "")
        model = e.get("model", "")
        error = e.get("error", "")
        output = e.get("output", "")

        status = "ERROR" if error else "OK"
        header = f"[{time}] {tid} ({mode}, {backend}/{model}) {status}"
        print(header)

        if error:
            print(f"  Error: {_summarize_prompt(error, 120)}")
        elif output:
            print(f"  Output: {_summarize_prompt(output, 120)}")
        print()
