"""CLI subcommands for managing schedules in a single YAML file."""

import json
import sys
from pathlib import Path

from boxagent.utils import safe_print as _safe_print

import yaml
from croniter import croniter
from boxagent.paths import default_config_dir, default_local_dir
from boxagent.scheduler.engine import (
    DEFAULT_ISOLATE_TIMEOUT_SECONDS,
    SCHEDULE_NODE_OVERRIDES_KEY,
    load_schedule_entries,
)


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
    add.add_argument("--model", default="", help="Model override (empty = default)")
    add.add_argument(
        "--timeout-seconds",
        default=DEFAULT_ISOLATE_TIMEOUT_SECONDS,
        type=float,
        help=f"Isolate timeout in seconds (default: {DEFAULT_ISOLATE_TIMEOUT_SECONDS:g})",
    )
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
    logs.add_argument("--run", type=int, default=0, help="Show full detail for the Nth most recent run (1 = latest, requires --id)")
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


def add_schedule(
    config_dir: str | Path,
    task_id: str,
    cron: str,
    prompt: str,
    *,
    mode: str = "isolate",
    bot: str = "",
    ai_backend: str = "",
    model: str = "",
    timeout_seconds: float = DEFAULT_ISOLATE_TIMEOUT_SECONDS,
    enabled_on_nodes: str = "",
    enabled: bool = True,
) -> str:
    """Add a new schedule entry. Returns status message."""
    if task_id == SCHEDULE_NODE_OVERRIDES_KEY:
        return f"Error: '{SCHEDULE_NODE_OVERRIDES_KEY}' is a reserved schedule id."

    if not croniter.is_valid(cron):
        return f"Error: invalid cron expression '{cron}'."

    if mode == "append" and not bot:
        return "Error: bot is required when mode=append."
    if mode == "isolate" and not ai_backend:
        return "Error: ai_backend is required when mode=isolate."
    if timeout_seconds <= 0:
        return "Error: timeout_seconds must be > 0."

    path = Path(config_dir) / "schedules.yaml"
    all_scheds = _load_all(path)

    if task_id in all_scheds:
        return f"Error: schedule '{task_id}' already exists."

    entry = {
        "cron": cron,
        "prompt": prompt,
        "mode": mode,
        "bot": bot,
        "ai_backend": ai_backend,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "enabled_on_nodes": enabled_on_nodes,
        "enabled": enabled,
    }
    all_scheds[task_id] = entry
    _save_all(path, all_scheds)
    return f"Created schedule '{task_id}'."


def schedule_add(args) -> None:
    """Add a new schedule entry."""
    result = add_schedule(
        config_dir=_schedules_file(args).parent,
        task_id=args.id,
        cron=args.cron,
        prompt=args.prompt,
        mode=args.mode,
        bot=args.bot,
        ai_backend=args.ai_backend,
        model=args.model,
        timeout_seconds=float(getattr(args, "timeout_seconds", DEFAULT_ISOLATE_TIMEOUT_SECONDS)),
        enabled_on_nodes=args.enabled_on_nodes,
        enabled=args.enabled,
    )
    _safe_print(result)
    if result.startswith("Error:"):
        sys.exit(1)


def format_schedule_list(config_dir: str | Path, node_id: str = "") -> str:
    """Return a formatted string listing all schedules."""
    path = Path(config_dir) / "schedules.yaml"
    all_scheds = load_schedule_entries(path, node_id=node_id)

    if not all_scheds:
        return "No schedules found."

    lines = []
    for task_id, entry in all_scheds.items():
        cron = entry.get("cron", "?")
        mode = entry.get("mode", "isolate")
        enabled = "on" if entry.get("enabled", True) else "off"
        prompt = _summarize_prompt(entry.get("prompt", ""), 50)
        ai_backend = entry.get("ai_backend", "")
        model = entry.get("model", "")
        enabled_on_nodes = entry.get("enabled_on_nodes", "")

        # Build backend/model suffix
        if ai_backend and model:
            backend_info = f" {ai_backend}/{model}"
        elif ai_backend:
            backend_info = f" {ai_backend}"
        elif model:
            backend_info = f" {model}"
        else:
            backend_info = ""

        lines.append(f"`{task_id}` {enabled} `{cron}` ({mode}){backend_info}")
        if prompt:
            lines.append(f"  {prompt}")
        if enabled_on_nodes:
            if isinstance(enabled_on_nodes, list):
                nodes_str = ", ".join(str(n) for n in enabled_on_nodes)
            else:
                nodes_str = str(enabled_on_nodes)
            lines.append(f"  nodes: {nodes_str}")
    return "\n".join(lines)


def schedule_list(args) -> None:
    """List all schedules."""
    _safe_print(format_schedule_list(
        _schedules_file(args).parent,
        node_id=_load_node_id(args),
    ))


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


def format_schedule_show(config_dir: str | Path, node_id: str, task_id: str) -> str:
    """Return formatted details for a single schedule."""
    path = Path(config_dir) / "schedules.yaml"
    all_scheds = load_schedule_entries(path, node_id=node_id)

    if task_id not in all_scheds:
        return f"Schedule '{task_id}' not found."

    entry = {task_id: all_scheds[task_id]}
    return yaml.dump(
        entry, Dumper=_ScheduleDumper, default_flow_style=False,
        sort_keys=False, allow_unicode=False,
    ).rstrip()


def schedule_show(args) -> None:
    """Show a schedule's details."""
    _safe_print(format_schedule_show(
        _schedules_file(args).parent,
        node_id=_load_node_id(args),
        task_id=args.id,
    ))


def trigger_schedule_run(local_dir: str | Path, task_id: str, sync: bool = False) -> str:
    """Trigger a schedule run via gateway HTTP API. Returns status message."""
    import httpx

    local_dir = Path(local_dir)
    sock_path = local_dir / "api.sock"
    timeout = 10.0 if not sync else 300.0

    targets = []
    if sock_path.exists():
        targets.append(
            (httpx.HTTPTransport(uds=str(sock_path)), "http://localhost/api/schedule/run")
        )
    port_file = local_dir / API_PORT_FILE
    if port_file.is_file():
        try:
            port = int(port_file.read_text(encoding="utf-8").strip())
            if port:
                targets.append(
                    (httpx.HTTPTransport(), f"http://127.0.0.1:{port}/api/schedule/run")
                )
        except (OSError, ValueError):
            pass

    if not targets:
        return "Error: gateway not running."

    payload = {"id": task_id}
    if not sync:
        payload["async"] = True

    for transport, url in targets:
        try:
            with httpx.Client(transport=transport, timeout=timeout) as client:
                resp = client.post(url, json=payload)
            data = resp.json()
            if data.get("ok"):
                if not sync:
                    return f"Schedule '{task_id}' triggered (async)."
                output = data.get("output", "")
                return output if output else f"Schedule '{task_id}' completed (no output)."
            return f"Error: {data.get('error', 'unknown')}"
        except httpx.ConnectError:
            continue

    return "Error: gateway not running."


def schedule_run(args) -> None:
    """Run a schedule once immediately via the gateway HTTP API."""
    result = trigger_schedule_run(
        _local_dir(args), args.id, sync=getattr(args, "sync", False),
    )
    _safe_print(result)
    if result.startswith("Error:"):
        sys.exit(1)


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
            with open(f, encoding="utf-8", errors="replace") as fh:
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


def format_schedule_logs(local_dir: str | Path, task_id: str = "", n: int = 20) -> str:
    """Return a formatted string of schedule execution logs."""
    entries = _load_run_logs(Path(local_dir), task_id=task_id)

    if not entries:
        if task_id:
            return f"No logs found for '{task_id}'."
        return "No schedule logs found."

    entries = entries[:n]
    lines = []
    for e in entries:
        t = e.get("time", "?")
        tid = e.get("task", "?")
        mode = e.get("mode", "?")
        backend = e.get("ai_backend", "")
        model = e.get("model", "")
        node = e.get("node_id", "")
        error = e.get("error", "")
        output = e.get("output", "")
        result = e.get("result")

        status = "ERROR" if error else "OK"
        node_suffix = f" @{node}" if node else ""
        lines.append(f"[{t}] {tid} ({mode}, {backend}/{model}) {status}{node_suffix}")

        if error:
            lines.append(f"  Error: {_summarize_prompt(error, 120)}")
        elif isinstance(result, str) and result:
            lines.append(f"  Result: {_summarize_prompt(result, 120)}")
        elif output:
            lines.append(f"  Output: {_summarize_prompt(output, 120)}")
    return "\n".join(lines)


def format_schedule_run_detail(local_dir: str | Path, task_id: str, run_index: int = 1) -> str:
    """Return full details for a single run log entry.

    Args:
        local_dir: Path to local data directory
        task_id: Task ID to look up
        run_index: 1-indexed run number (1 = most recent), default 1
    """
    entries = _load_run_logs(Path(local_dir), task_id=task_id)
    if not entries:
        return f"No logs found for '{task_id}'."

    if run_index < 1 or run_index > len(entries):
        return f"Run #{run_index} not found. '{task_id}' has {len(entries)} run(s)."

    e = entries[run_index - 1]
    lines = [f"**Run #{run_index}** of `{task_id}`", ""]

    for key in ("time", "node_id", "mode", "ai_backend", "model", "bot", "workspace", "timeout_seconds"):
        val = e.get(key, "")
        if val not in ("", None):
            lines.append(f"**{key}**: {val}")

    error = e.get("error", "")
    result = e.get("result", "")
    output = e.get("output", "")

    lines.append(f"**status**: {'ERROR' if error else 'OK'}")
    lines.append("")

    if error:
        lines.append("**Error:**")
        lines.append(error)
    if result:
        lines.append("**Result:**")
        lines.append(str(result))
    if output:
        lines.append("**Output:**")
        lines.append(output)

    return "\n".join(lines)


def schedule_logs(args) -> None:
    """Show schedule execution logs."""
    task_id = getattr(args, "id", "")
    n = getattr(args, "lines", 20)
    run_index = getattr(args, "run", 0)

    if run_index > 0 and task_id:
        _safe_print(format_schedule_run_detail(_local_dir(args), task_id, run_index))
        return

    if getattr(args, "output_json", False):
        entries = _load_run_logs(_local_dir(args), task_id=task_id)[:n]
        _safe_print(json.dumps(entries, indent=2, ensure_ascii=False))
        return

    _safe_print(format_schedule_logs(_local_dir(args), task_id=task_id, n=n))
