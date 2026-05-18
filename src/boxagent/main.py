"""BoxAgent entry point."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from boxagent.config import load_config, ConfigError
from boxagent.gateway import Gateway
from boxagent.utils import default_config_dir, default_local_dir

logger = logging.getLogger(__name__)


def _run_doctor(args) -> None:
    from boxagent.doctor import run_doctor
    ba_dir = _resolve_ba_dir(args)
    run_doctor(ba_dir, fix=getattr(args, "fix", False))


def _run_install(args) -> None:
    from boxagent.doctor import run_doctor
    ba_dir = _resolve_ba_dir(args)
    run_doctor(ba_dir, fix=True)


def _resolve_ba_dir(args) -> Path:
    from boxagent.utils import resolve_boxagent_dir
    return resolve_boxagent_dir(getattr(args, "box_agent_dir", None))


def main():
    from boxagent._version import version_string

    parser = argparse.ArgumentParser(
        description="BoxAgent (BA) gateway"
    )
    parser.add_argument(
        "--version", "-V", action="version",
        version=version_string(),
    )
    parser.add_argument(
        "--box-agent-dir", "--ba-dir",
        dest="box_agent_dir",
        type=Path,
        default=None,
        help=(
            "Override the BA config directory. Defaults to "
            "BOX_AGENT_DIR/BOXAGENT_DIR, then legacy "
            "BOX_AGENT_HOME/BOXAGENT_HOME, then ~/.boxagent."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Config directory override. Defaults to the BA directory.",
    )
    parser.add_argument(
        "--log-file", type=Path, default=None,
        help="Log file path. Defaults to <local-dir>/boxagent.log.",
    )

    subparsers = parser.add_subparsers(dest="command")

    # Register schedule subcommands
    from boxagent.scheduler.cli import build_schedule_parser
    build_schedule_parser(subparsers)

    # Top-level doctor
    doc = subparsers.add_parser("doctor", help="Check environment, dependencies, and config")
    doc.add_argument("--fix", action="store_true", default=False,
                     help="Auto-install missing dependencies")
    doc.set_defaults(func=lambda args: _run_doctor(args))

    # Top-level install (alias for doctor --fix)
    install_parser = subparsers.add_parser("install", help="Install missing dependencies (alias for doctor --fix)")
    install_parser.set_defaults(func=lambda args: _run_install(args))

    args = parser.parse_args()

    # Dispatch to schedule CLI (no daemon, no config loading)
    if args.command == "schedule":
        if hasattr(args, "func"):
            args.func(args)
        else:
            parser.parse_args(["schedule", "--help"])
        return

    # Dispatch to top-level doctor / install
    if args.command in ("doctor", "install"):
        args.func(args)
        return

    config_dir = args.config or default_config_dir(args.box_agent_dir)
    local_dir = default_local_dir(args.box_agent_dir)

    # Determine log file
    log_file = args.log_file
    if log_file is None:
        local_dir.mkdir(parents=True, exist_ok=True)
        log_file = local_dir / "boxagent.log"

    # Default: run daemon — log to both file and stderr
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))

    class _JsonLineFormatter(logging.Formatter):
        # Hand-rolled JSON templates break when msg contains quotes or newlines
        # (every aiohttp.access line and every traceback). Use json.dumps so the
        # Logs page can parse every line cleanly.
        def format(self, record: logging.LogRecord) -> str:
            import json as _json
            payload = {
                "time": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["msg"] += "\n" + self.formatException(record.exc_info)
            return _json.dumps(payload, ensure_ascii=False)

    formatter = _JsonLineFormatter()
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)

    try:
        config = load_config(config_dir, box_agent_dir=args.box_agent_dir, local_dir=local_dir)
        config.log_file = Path(log_file) if log_file else None
    except ConfigError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)

    logging.getLogger().setLevel(
        getattr(logging, config.log_level.upper())
    )
    try:
        asyncio.run(_run(config, config_dir, local_dir))
    except KeyboardInterrupt:
        pass


async def _run(config, config_dir, local_dir):
    gateway = Gateway(config=config, config_dir=config_dir, local_dir=local_dir)
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()

    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    else:
        signal.signal(signal.SIGINT, lambda *_: stop.set())

    await gateway.start()
    await stop.wait()

    try:
        await asyncio.wait_for(gateway.stop(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Graceful shutdown timed out")


if __name__ == "__main__":
    main()
