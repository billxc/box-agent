"""Driver for manual E2E runs.

Usage:

    # Single backend, single scenario
    uv run python -m manual_e2e.run --backend claude-cli --scenario hello

    # Single backend, all scenarios
    uv run python -m manual_e2e.run --backend agent-sdk-claude --scenario all

    # All backends, one scenario (compare side-by-side)
    uv run python -m manual_e2e.run --backend all --scenario hello

    # Custom workspace + model
    uv run python -m manual_e2e.run --backend codex-cli --scenario hello \\
        --workspace /tmp --model gpt-5

The driver does NOT assert — it prints logs and a final summary. Read the
output (or hand it to an AI) and judge whether each backend behaved
correctly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from boxagent.agent.protocol import AgentBackend
from boxagent.config import BotConfig

from manual_e2e.harness import C, RecordingCallback, banner
from manual_e2e.scenarios import SCENARIOS


ALL_BACKENDS = ["claude-cli", "codex-cli", "agent-sdk-claude", "agent-sdk-copilot"]


def make_backend(kind: str, *, workspace: str, model: str, yolo: bool) -> AgentBackend:
    """Construct a backend by kind, reusing the production factory."""
    from boxagent.agent.manager import _create_backend

    cfg = BotConfig(
        name=f"manual-e2e-{kind}",
        ai_backend=kind,
        workspace=workspace,
        model=model,
        yolo=yolo,
    )
    return _create_backend(cfg, session_id=None)


async def run_scenario(
    backend_kind: str,
    scenario_name: str,
    *,
    workspace: str,
    model: str,
    yolo: bool,
    timeout: float,
) -> bool:
    """Run one scenario against one backend. Return True on no exception."""
    banner(f"{backend_kind}  ×  {scenario_name}")

    scenario = SCENARIOS[scenario_name]()
    backend = make_backend(backend_kind, workspace=workspace, model=model, yolo=yolo)
    cb = RecordingCallback(label=backend_kind)

    try:
        backend.start()
    except Exception as e:
        print(f"{C.RED}backend.start() raised: {e}{C.RESET}")
        return False

    overall_ok = True
    try:
        for i, (prompt, post_action) in enumerate(scenario, start=1):
            print(f"\n{C.BOLD}── Turn {i}/{len(scenario)} ──{C.RESET}")
            cb.begin_turn(prompt)

            send_task = asyncio.create_task(
                backend.send(prompt, cb, model=model)
            )
            post_task = (
                asyncio.create_task(post_action(backend))
                if post_action is not None else None
            )

            try:
                await asyncio.wait_for(send_task, timeout=timeout)
            except asyncio.TimeoutError:
                print(f"{C.RED}Turn timed out after {timeout}s — cancelling{C.RESET}")
                send_task.cancel()
                with _suppress():
                    await send_task
                overall_ok = False
            except asyncio.CancelledError:
                print(f"{C.YELLOW}Turn was cancelled{C.RESET}")
            except Exception as e:
                print(f"{C.RED}backend.send() raised: {e}{C.RESET}")
                overall_ok = False

            if post_task is not None and not post_task.done():
                with _suppress():
                    await post_task

            cb.end_turn()

            # Quick post-turn diagnostics
            if backend.last_turn_failed:
                print(
                    f"{C.RED}backend.last_turn_failed=True  "
                    f"error={backend.last_turn_error!r}{C.RESET}"
                )

            print(
                f"{C.DIM}post-turn: session_id={backend.session_id} "
                f"state={backend.state} text_len={len(cb.record.assistant_text)} "
                f"tool_calls={len(cb.record.tool_calls)}{C.RESET}"
            )
    finally:
        try:
            await backend.stop()
        except Exception as e:
            print(f"{C.YELLOW}backend.stop() raised: {e}{C.RESET}")

    return overall_ok


class _suppress:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return True  # swallow


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backend", default="claude-cli",
        help=f"Backend kind, one of {ALL_BACKENDS} or 'all' (default: claude-cli)",
    )
    parser.add_argument(
        "--scenario", default="hello",
        help=f"Scenario name from {sorted(SCENARIOS)} or 'all' (default: hello)",
    )
    parser.add_argument(
        "--workspace", default=None,
        help="Working directory for the backend (default: a fresh tmpdir)",
    )
    parser.add_argument("--model", default="", help="Model override (default: backend default)")
    parser.add_argument(
        "--yolo", action="store_true",
        help="Pass yolo=True (auto-approve tools). Strongly recommended.",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0,
        help="Per-turn timeout in seconds (default: 120)",
    )
    args = parser.parse_args()

    backends = ALL_BACKENDS if args.backend == "all" else [args.backend]
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]

    for b in backends:
        if b not in ALL_BACKENDS:
            print(f"unknown backend: {b}", file=sys.stderr)
            return 2
    for s in scenarios:
        if s not in SCENARIOS:
            print(f"unknown scenario: {s}", file=sys.stderr)
            return 2

    workspace = args.workspace or tempfile.mkdtemp(prefix="boxagent-e2e-")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    print(f"{C.DIM}workspace: {workspace}{C.RESET}")

    results: list[tuple[str, str, bool]] = []
    for backend_kind in backends:
        for scenario_name in scenarios:
            ok = await run_scenario(
                backend_kind, scenario_name,
                workspace=workspace, model=args.model, yolo=args.yolo,
                timeout=args.timeout,
            )
            results.append((backend_kind, scenario_name, ok))

    # Summary
    print()
    banner("SUMMARY")
    width = max(len(b) for b in backends) + 2
    for backend_kind, scenario_name, ok in results:
        marker = f"{C.GREEN}PASS{C.RESET}" if ok else f"{C.RED}FAIL{C.RESET}"
        print(f"  {marker}  {backend_kind:<{width}} {scenario_name}")

    failures = sum(1 for _, _, ok in results if not ok)
    if failures:
        print(f"\n{C.RED}{failures} run(s) raised an exception or timed out.{C.RESET}")
    print(
        f"\n{C.DIM}Note: 'PASS' means the run completed without raising; "
        f"the actual content quality is for you (or an AI) to judge from "
        f"the log above.{C.RESET}"
    )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
