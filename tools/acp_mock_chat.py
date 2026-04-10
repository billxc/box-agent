#!/usr/bin/env python3
"""Local mock chat receiver for backend debugging.

This harness is kept in `main` before ACP backend lands fully.
It already supports the current `claude-cli` backend, and can load ACP later
when `ACPProcess` becomes available in the checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

logger = logging.getLogger(__name__)


def _load_backend_process(backend: str):
    if backend == "claude-cli":
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI is not on PATH")
        from boxagent.agent.claude_process import ClaudeProcess  # type: ignore
        return ClaudeProcess

    if backend == "codex-acp":
        if not shutil.which("codex-acp"):
            raise RuntimeError("codex-acp is not on PATH")
        try:
            from boxagent.agent.acp_process import ACPProcess  # type: ignore
        except Exception as exc:  # pragma: no cover - runtime guard for pre-ACP main
            raise RuntimeError(
                "ACPProcess is not available in this checkout yet. "
                "Land FEAT005 / ACP backend implementation first, then rerun this tool."
            ) from exc
        return ACPProcess

    raise RuntimeError(f"Unsupported backend for mock harness: {backend}")


def _ts() -> float:
    return time.time()


def _json_default(obj: Any) -> str:
    return repr(obj)


@dataclass
class MockCallback:
    jsonl_path: Path | None = None
    print_events: bool = True
    text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, event: str, **data: Any) -> None:
        record = {"ts": _ts(), "event": event, **data}
        self.events.append(record)
        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        if self.print_events:
            print(line)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    async def on_stream(self, text: str) -> None:
        self.text += text
        self._record("stream", text=text)

    async def on_tool_call(self, name: str, input: dict, result: str) -> None:
        self._record("tool_call", name=name, input=input, result=result)

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self._record(
            "tool_update",
            tool_call_id=tool_call_id,
            title=title,
            status=status,
            input=input,
            output=output,
        )

    async def on_error(self, error: str) -> None:
        self._record("error", error=error)

    async def on_file(self, path: str, caption: str = "") -> None:
        self._record("file", path=path, caption=caption)

    async def on_image(self, path: str, caption: str = "") -> None:
        self._record("image", path=path, caption=caption)


def build_turn_summary(
    prompt: str,
    cb: MockCallback,
    *,
    timeout: bool = False,
    exc: str = "",
) -> dict[str, Any]:
    tool_calls = [e for e in cb.events if e["event"] == "tool_call"]
    tool_updates = [e for e in cb.events if e["event"] == "tool_update"]
    tool_statuses = [e.get("status") for e in tool_updates]
    unique_tool_ids = sorted(
        {e.get("tool_call_id") for e in tool_updates if e.get("tool_call_id")}
    )
    has_none_status = any(s is None for s in tool_statuses)
    return {
        "prompt": prompt,
        "final_text": cb.text,
        "final_text_len": len(cb.text),
        "event_count": len(cb.events),
        "tool_call_count": len(tool_calls),
        "tool_update_count": len(tool_updates),
        "tool_statuses": tool_statuses,
        "unique_tool_ids": unique_tool_ids,
        "has_error_event": any(e["event"] == "error" for e in cb.events),
        "has_none_tool_status": has_none_status,
        "timeout": timeout,
        "exception": exc,
        "events": cb.events,
    }


async def run_case(
    name: str,
    prompts: list[str],
    *,
    backend: str,
    workspace: str,
    model: str,
    acp_command: str,
    timeout_sec: float,
    jsonl_dir: Path | None,
    print_events: bool,
) -> dict[str, Any]:
    Process = _load_backend_process(backend)
    kwargs: dict[str, Any] = {"workspace": workspace, "model": model}
    if backend == "codex-acp":
        kwargs["acp_command"] = acp_command
    proc = Process(**kwargs)
    proc.start()
    case: dict[str, Any] = {"case": name, "backend": backend, "turns": []}
    try:
        for idx, prompt in enumerate(prompts, 1):
            jsonl_path = None
            if jsonl_dir:
                jsonl_path = jsonl_dir / f"{name}-turn{idx}.jsonl"
            cb = MockCallback(jsonl_path=jsonl_path, print_events=print_events)
            timeout = False
            exc = ""
            try:
                await asyncio.wait_for(
                    proc.send(prompt, cb, model=model), timeout=timeout_sec
                )
            except asyncio.TimeoutError:
                timeout = True
                exc = f"timeout after {timeout_sec}s"
                await cb.on_error(exc)
                try:
                    await proc.cancel()
                except Exception as cancel_exc:  # pragma: no cover - debug aid
                    exc += f"; cancel failed: {cancel_exc}"
            except Exception as run_exc:  # pragma: no cover - debug aid
                exc = repr(run_exc)
                await cb.on_error(exc)
            case["turns"].append(
                build_turn_summary(prompt, cb, timeout=timeout, exc=exc)
            )
    finally:
        await proc.stop()
    return case


def load_suite(path: Path) -> list[tuple[str, list[str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[tuple[str, list[str]]] = []
    for item in data:
        if item.get("disabled"):
            continue
        name = str(item["name"])
        prompts = item.get("prompts") or []
        if isinstance(prompts, str):
            prompts = [prompts]
        cases.append((name, [str(p) for p in prompts]))
    return cases


def print_case_summary(case: dict[str, Any]) -> None:
    backend = case.get("backend", "unknown")
    print(f"=== {case['case']} ({backend}) ===")
    if case.get("fatal_error"):
        print("FATAL_ERROR:", case["fatal_error"])
        print()
        return
    for idx, turn in enumerate(case["turns"], 1):
        print(f"TURN {idx} prompt: {turn['prompt']}")
        print(f"FINAL_LEN: {turn['final_text_len']}")
        print("FINAL:", repr(turn["final_text"][:500]))
        print("TOOL_CALL_COUNT:", turn["tool_call_count"])
        print("TOOL_STATUSES:", turn["tool_statuses"])
        print("UNIQUE_TOOL_IDS:", turn["unique_tool_ids"])
        print("HAS_NONE_TOOL_STATUS:", turn["has_none_tool_status"])
        print("HAS_ERROR_EVENT:", turn["has_error_event"])
        print("TIMEOUT:", turn["timeout"])
        print("EXCEPTION:", turn["exception"])
        print()


async def _amain(args: argparse.Namespace) -> int:
    jsonl_dir = Path(args.jsonl_dir) if args.jsonl_dir else None

    if args.suite_json:
        cases = load_suite(Path(args.suite_json))
    else:
        prompt = args.prompt or Path(args.prompt_file).read_text(encoding="utf-8")
        cases = [(args.case_name or "single", [prompt])]

    results = []
    for name, prompts in cases:
        try:
            result = await run_case(
                name,
                prompts,
                backend=args.backend,
                workspace=args.workspace,
                model=args.model,
                acp_command=args.acp_command,
                timeout_sec=args.timeout,
                jsonl_dir=jsonl_dir,
                print_events=args.print_events,
            )
        except Exception as exc:  # pragma: no cover - hard failure isolation
            result = {
                "case": name,
                "backend": args.backend,
                "turns": [],
                "fatal_error": repr(exc),
            }
        results.append(result)
        print_case_summary(result)

    if args.result_json:
        out = Path(args.result_json)
        out.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"Wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real backend with a local mock chat receiver"
    )
    parser.add_argument("prompt", nargs="?", default="", help="Prompt to send to ACP backend")
    parser.add_argument("--prompt-file", default="", help="Read single prompt from file")
    parser.add_argument(
        "--suite-json",
        default="",
        help="JSON file with case list: [{name,prompts}]",
    )
    parser.add_argument(
        "--case-name", default="single", help="Case name for single prompt mode"
    )
    parser.add_argument(
        "--backend",
        default="claude-cli",
        choices=["claude-cli", "codex-acp"],
        help="Backend to run inside the harness",
    )
    parser.add_argument(
        "--workspace",
        default="/tmp/acp-test",
        help="Workspace directory passed to the backend",
    )
    parser.add_argument("--model", default="", help="Optional model override")
    parser.add_argument(
        "--acp-command",
        default="codex-acp",
        help="ACP agent command to spawn when --backend=codex-acp",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-turn timeout in seconds",
    )
    parser.add_argument(
        "--jsonl-dir",
        default="",
        help="Optional directory to append per-turn structured events",
    )
    parser.add_argument(
        "--result-json",
        default="",
        help="Optional JSON file to write summarized results",
    )
    parser.add_argument(
        "--no-print-events",
        dest="print_events",
        action="store_false",
        help="Do not print every raw event; only print summaries",
    )
    parser.set_defaults(print_events=True)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if not args.suite_json and not args.prompt and not args.prompt_file:
        parser.error("provide a prompt, --prompt-file, or --suite-json")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
