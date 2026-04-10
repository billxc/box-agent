"""Unit tests for rule-based harness judging."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.judge_harness_results import build_judgement


class TestHarnessJudge:
    def test_passes_when_expected_substrings_match(self):
        suite = [
            {
                "name": "basic_chat",
                "expect": {
                    "turn": "last",
                    "must_not_be_empty": True,
                    "must_contain": ["harness ok"],
                },
            }
        ]
        results = [
            {
                "case": "basic_chat",
                "turns": [
                    {
                        "final_text": "harness ok",
                        "tool_call_count": 0,
                        "tool_statuses": [],
                        "timeout": False,
                        "has_error_event": False,
                        "exception": "",
                    }
                ],
            }
        ]

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["pass"] == 1
        assert judgement["cases"][0]["verdict"] == "pass"

    def test_fails_when_required_substring_missing(self):
        suite = [
            {
                "name": "session_memory",
                "expect": {
                    "turn": "last",
                    "must_contain": ["banana42"],
                },
            }
        ]
        results = [
            {
                "case": "session_memory",
                "turns": [
                    {
                        "final_text": "I forgot it.",
                        "tool_call_count": 0,
                        "tool_statuses": [],
                        "timeout": False,
                        "has_error_event": False,
                        "exception": "",
                    }
                ],
            }
        ]

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["fail"] == 1
        assert judgement["cases"][0]["verdict"] == "fail"

    def test_needs_review_when_no_expect_block(self):
        suite = [{"name": "adhoc"}]
        results = [{"case": "adhoc", "turns": []}]

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["needs_review"] == 1
        assert judgement["cases"][0]["verdict"] == "needs_review"

    def test_disabled_case_is_reported_and_not_treated_as_fail(self):
        suite = [{"name": "single_char_probe", "disabled": True, "disabled_reason": "not fixed yet"}]
        results = []

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["disabled"] == 1
        assert judgement["cases"][0]["verdict"] == "disabled"
        assert judgement["cases"][0]["reason"] == "not fixed yet"

    def test_fails_when_tool_call_required_but_missing(self):
        suite = [
            {
                "name": "pwd",
                "expect": {
                    "turn": "last",
                    "must_have_tool_call": True,
                    "must_not_be_empty": True,
                },
            }
        ]
        results = [
            {
                "case": "pwd",
                "turns": [
                    {
                        "final_text": "/tmp/test\n",
                        "tool_call_count": 0,
                        "tool_update_count": 0,
                        "tool_statuses": [],
                        "timeout": False,
                        "has_error_event": False,
                        "exception": "",
                    }
                ],
            }
        ]

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["fail"] == 1
        assert judgement["cases"][0]["reason"] == "expected at least one tool call or tool lifecycle update"

    def test_passes_when_tool_update_present_even_without_legacy_tool_call(self):
        suite = [
            {
                "name": "pwd",
                "expect": {
                    "turn": "last",
                    "must_have_tool_call": True,
                    "must_not_be_empty": True,
                },
            }
        ]
        results = [
            {
                "case": "pwd",
                "turns": [
                    {
                        "final_text": "/tmp/acp-test\n",
                        "tool_call_count": 0,
                        "tool_update_count": 2,
                        "tool_statuses": ["in_progress", "completed"],
                        "timeout": False,
                        "has_error_event": False,
                        "exception": "",
                    }
                ],
            }
        ]

        judgement = build_judgement(suite, results)

        assert judgement["summary"]["pass"] == 1
        assert judgement["cases"][0]["verdict"] == "pass"
