#!/usr/bin/env python3
"""Judge harness result JSON against suite expectations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _case_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["name"]): item for item in items}


def _pick_turn(case_result: dict[str, Any], turn_spec: Any) -> dict[str, Any] | None:
    turns = case_result.get("turns") or []
    if not turns:
        return None
    if turn_spec in (None, "last"):
        return turns[-1]
    if isinstance(turn_spec, int):
        idx = turn_spec - 1
        if 0 <= idx < len(turns):
            return turns[idx]
        return None
    return None


def _contains_all(text: str, needles: list[str]) -> bool:
    return all(n.lower() in text for n in needles)


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(n.lower() in text for n in needles)


def judge_case(
    case_def: dict[str, Any],
    case_result: dict[str, Any] | None,
) -> dict[str, Any]:
    name = str(case_def["name"])
    if case_def.get("disabled"):
        return {
            "name": name,
            "verdict": "disabled",
            "reason": str(case_def.get("disabled_reason") or "case disabled"),
            "evidence": {},
        }

    expect = case_def.get("expect")
    if not expect:
        return {
            "name": name,
            "verdict": "needs_review",
            "reason": "no expectation schema defined for this case",
            "evidence": {},
        }

    if case_result is None:
        return {
            "name": name,
            "verdict": "fail",
            "reason": "case missing from result file",
            "evidence": {},
        }

    if case_result.get("fatal_error"):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "runner hit fatal error before producing turns",
            "evidence": {"fatal_error": case_result.get("fatal_error")},
        }

    turn = _pick_turn(case_result, expect.get("turn", "last"))
    if turn is None:
        return {
            "name": name,
            "verdict": "fail",
            "reason": "expected turn not present in result file",
            "evidence": {"turns": len(case_result.get("turns") or [])},
        }

    final_text = str(turn.get("final_text") or "")
    final_text_norm = final_text.lower()
    evidence = {
        "final_text": final_text[:500],
        "tool_call_count": turn.get("tool_call_count"),
        "tool_update_count": turn.get("tool_update_count"),
        "tool_statuses": turn.get("tool_statuses"),
        "timeout": turn.get("timeout"),
        "has_error_event": turn.get("has_error_event"),
        "exception": turn.get("exception"),
    }

    if expect.get("must_not_timeout") and turn.get("timeout"):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "turn timed out",
            "evidence": evidence,
        }

    if expect.get("must_not_error_event") and turn.get("has_error_event"):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "turn emitted error event",
            "evidence": evidence,
        }

    if expect.get("must_not_be_empty") and not final_text.strip():
        return {
            "name": name,
            "verdict": "fail",
            "reason": "final text is empty",
            "evidence": evidence,
        }

    tool_call_count = int(turn.get("tool_call_count") or 0)
    tool_update_count = int(turn.get("tool_update_count") or 0)
    if expect.get("must_have_tool_call") and (tool_call_count <= 0 and tool_update_count <= 0):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "expected at least one tool call or tool lifecycle update",
            "evidence": evidence,
        }

    must_contain = expect.get("must_contain") or []
    if must_contain and not _contains_all(final_text_norm, [str(x) for x in must_contain]):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "final text missing required substring(s)",
            "evidence": {**evidence, "must_contain": must_contain},
        }

    must_contain_any = expect.get("must_contain_any") or []
    if must_contain_any and not _contains_any(final_text_norm, [str(x) for x in must_contain_any]):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "final text missing any acceptable substring",
            "evidence": {**evidence, "must_contain_any": must_contain_any},
        }

    must_not_contain = expect.get("must_not_contain") or []
    if must_not_contain and _contains_any(final_text_norm, [str(x) for x in must_not_contain]):
        return {
            "name": name,
            "verdict": "fail",
            "reason": "final text contains forbidden substring",
            "evidence": {**evidence, "must_not_contain": must_not_contain},
        }

    return {
        "name": name,
        "verdict": "pass",
        "reason": "all rule-based checks passed",
        "evidence": evidence,
    }


def build_judgement(suite: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    result_map = {str(item.get("case")): item for item in results}
    cases = [judge_case(case_def, result_map.get(str(case_def["name"]))) for case_def in suite]
    summary = {"pass": 0, "fail": 0, "needs_review": 0, "disabled": 0}
    for case in cases:
        summary[case["verdict"]] += 1
    return {
        "summary": summary,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge harness result JSON against suite expectations")
    parser.add_argument("--suite-json", required=True, help="Suite definition JSON with expect blocks")
    parser.add_argument("--result-json", required=True, help="Result JSON produced by tools/acp_mock_chat.py")
    parser.add_argument("--judge-json", default="", help="Optional output JSON path for verdicts")
    args = parser.parse_args()

    suite = _load_json(args.suite_json)
    results = _load_json(args.result_json)
    judgement = build_judgement(suite, results)

    print(json.dumps(judgement, ensure_ascii=False, indent=2))
    if args.judge_json:
        Path(args.judge_json).write_text(
            json.dumps(judgement, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
