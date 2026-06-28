"""Unit tests for workgroup.formatting."""

import time

from boxagent.workgroup.formatting import (
    extract_specialist_response,
    format_running_tasks,
)

class TestFormatRunningTasks:
    def test_empty_list(self):
        assert format_running_tasks([]) == "No specialist tasks currently running."

    def test_none(self):
        assert format_running_tasks(None) == "No specialist tasks currently running."

    def test_single_active_task(self):
        tasks = [
            {"task_id": "dev-1", "target": "dev", "started_at": time.time() - 90, "active": True},
        ]
        result = format_running_tasks(tasks)
        assert "dev-1" in result
        assert "dev" in result
        assert "[active]" in result
        assert "1m" in result

    def test_queued_task(self):
        tasks = [
            {"task_id": "pm-2", "target": "pm", "started_at": time.time() - 30, "active": False},
        ]
        result = format_running_tasks(tasks)
        assert "[queued]" in result

    def test_no_started_at(self):
        tasks = [{"task_id": "x-1", "target": "x", "active": False}]
        result = format_running_tasks(tasks)
        assert "x-1" in result
        # No elapsed time shown when started_at is missing
        assert "(running" not in result


class TestExtractSpecialistResponse:
    def test_with_tags(self):
        text = "Thinking...\n<specialist_response>\nDone. Fixed the bug.\n</specialist_response>"
        assert extract_specialist_response(text) == "Done. Fixed the bug."

    def test_without_tags_fallback(self):
        text = "Just a plain response"
        assert extract_specialist_response(text) == "Just a plain response"

    def test_extra_content_after_tags(self):
        text = "<specialist_response>Result here</specialist_response>\ntrailing"
        assert extract_specialist_response(text) == "Result here"

    def test_multiline_content(self):
        text = "<specialist_response>\nLine 1\nLine 2\nLine 3\n</specialist_response>"
        assert extract_specialist_response(text) == "Line 1\nLine 2\nLine 3"

    def test_empty_tags(self):
        text = "<specialist_response></specialist_response>"
        assert extract_specialist_response(text) == ""

