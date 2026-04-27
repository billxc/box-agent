"""Tests for schedule_cli — CLI subcommand handlers."""

from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from boxagent.scheduler.engine import DEFAULT_ISOLATE_TIMEOUT_SECONDS
from boxagent.scheduler.cli import (
    _load_run_logs,
    _safe_print,
    _save_all,
    _schedules_file,
    schedule_add,
    schedule_del,
    schedule_disable,
    schedule_enable,
    schedule_list,
    schedule_logs,
    schedule_run,
    schedule_show,
)


def _make_args(tmp_path, **kwargs):
    """Create a Namespace with config pointed to tmp_path."""
    defaults = {"config": tmp_path}
    defaults.update(kwargs)
    return Namespace(**defaults)


def _write_sched(tmp_path, task_id, **overrides):
    path = tmp_path / "schedules.yaml"
    existing = {}
    if path.is_file():
        with open(path) as f:
            existing = yaml.safe_load(f) or {}
    entry = {
        "cron": "0 9 * * *",
        "prompt": "Test prompt",
        "mode": "isolate",
        "bot": "",
        "ai_backend": "claude-cli",
        "model": "sonnet",
        "timeout_seconds": DEFAULT_ISOLATE_TIMEOUT_SECONDS,
        "enabled_on_nodes": "",
        "enabled": True,
    }
    entry.update(overrides)
    existing[task_id] = entry
    with open(path, "w") as f:
        yaml.safe_dump(existing, f)
    return path


def _read_sched(tmp_path, task_id):
    path = tmp_path / "schedules.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return data[task_id]


def _write_node_id(tmp_path, node_id: str) -> None:
    local_dir = tmp_path / "local"
    local_dir.mkdir(exist_ok=True)
    (local_dir / "local.yaml").write_text(
        f"node_id: {node_id}\n",
        encoding="utf-8",
    )


# --- schedule_add ---


def test_add_creates_entry(tmp_path):
    args = _make_args(
        tmp_path,
        id="my-task",
        cron="0 9 * * *",
        prompt="Do something",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
    )
    schedule_add(args)

    path = tmp_path / "schedules.yaml"
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert "my-task" in data
    assert data["my-task"]["cron"] == "0 9 * * *"
    assert data["my-task"]["prompt"] == "Do something"
    assert data["my-task"]["enabled"] is True
    assert data["my-task"]["timeout_seconds"] == DEFAULT_ISOLATE_TIMEOUT_SECONDS


def test_add_persists_custom_timeout_seconds(tmp_path):
    args = _make_args(
        tmp_path,
        id="my-task",
        cron="0 9 * * *",
        prompt="Do something",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        timeout_seconds=42.5,
        enabled_on_nodes="",
        enabled=True,
    )
    schedule_add(args)

    data = yaml.safe_load((tmp_path / "schedules.yaml").read_text())
    assert data["my-task"]["timeout_seconds"] == 42.5


def test_add_multiline_prompt_uses_block_scalar(tmp_path):
    args = _make_args(
        tmp_path,
        id="multi-line",
        cron="0 9 * * *",
        prompt="Line one\nLine two\nLine three",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
    )
    schedule_add(args)

    content = (tmp_path / "schedules.yaml").read_text()
    assert "prompt: |-" in content or "prompt: |" in content
    assert "  Line one" in content
    assert "  Line two" in content


def test_add_invalid_cron(tmp_path):
    args = _make_args(
        tmp_path,
        id="bad-cron",
        cron="not-valid",
        prompt="Do it",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
    )
    with pytest.raises(SystemExit):
        schedule_add(args)


def test_add_append_requires_bot(tmp_path):
    args = _make_args(
        tmp_path,
        id="no-bot",
        cron="0 9 * * *",
        prompt="Do it",
        mode="append",
        bot="",
        ai_backend="",
        model="",
        enabled_on_nodes="",
        enabled=True,
    )
    with pytest.raises(SystemExit):
        schedule_add(args)




def test_add_isolate_requires_ai_backend(tmp_path):
    args = _make_args(
        tmp_path,
        id="no-backend",
        cron="0 9 * * *",
        prompt="Do it",
        mode="isolate",
        bot="",
        ai_backend="",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
    )
    with pytest.raises(SystemExit):
        schedule_add(args)


def test_add_rejects_non_positive_timeout(tmp_path):
    args = _make_args(
        tmp_path,
        id="bad-timeout",
        cron="0 9 * * *",
        prompt="Do it",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        timeout_seconds=0,
        enabled_on_nodes="",
        enabled=True,
    )
    with pytest.raises(SystemExit):
        schedule_add(args)


def test_add_duplicate_rejected(tmp_path):
    _write_sched(tmp_path, "dup-task")
    args = _make_args(
        tmp_path,
        id="dup-task",
        cron="0 9 * * *",
        prompt="Duplicate",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
    )
    with pytest.raises(SystemExit):
        schedule_add(args)


def test_add_preserves_node_overrides_block(tmp_path):
    path = tmp_path / "schedules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "node_overrides": {
                    "my-server": {
                        "xl-only": {
                            "cron": "0 10 * * *",
                            "prompt": "Only on XL",
                            "mode": "isolate",
                            "bot": "",
                            "ai_backend": "claude-cli",
                            "model": "sonnet",
                            "enabled": True,
                        }
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    args = _make_args(
        tmp_path,
        id="base-task",
        cron="0 9 * * *",
        prompt="Do something",
        mode="isolate",
        bot="",
        ai_backend="claude-cli",
        model="sonnet",
        enabled_on_nodes="",
        enabled=True,
        box_agent_dir=tmp_path,
    )

    schedule_add(args)

    data = yaml.safe_load(path.read_text())
    assert "base-task" in data
    assert "node_overrides" in data
    assert "xl-only" in data["node_overrides"]["my-server"]


# --- schedule_list ---


def test_list_empty(tmp_path, capsys):
    args = _make_args(tmp_path)
    schedule_list(args)
    assert "No schedules" in capsys.readouterr().out


def test_list_shows_all(tmp_path, capsys):
    _write_sched(tmp_path, "task-1", prompt="First task")
    _write_sched(tmp_path, "task-2", prompt="Second task")
    args = _make_args(tmp_path)
    schedule_list(args)
    out = capsys.readouterr().out
    assert "task-1" in out
    assert "task-2" in out


def test_list_flattens_multiline_prompt(tmp_path, capsys):
    _write_sched(tmp_path, "task-1", prompt="First line\nSecond line")
    args = _make_args(tmp_path)
    schedule_list(args)
    out = capsys.readouterr().out
    assert "First line Second line" in out
    assert "First line\nSecond line" not in out


def test_list_applies_node_overrides_for_current_node(tmp_path, capsys):
    path = tmp_path / "schedules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "task-1": {
                    "cron": "0 9 * * *",
                    "prompt": "Base prompt",
                    "mode": "isolate",
                    "bot": "",
                    "ai_backend": "claude-cli",
                    "model": "sonnet",
                    "enabled": True,
                },
                "node_overrides": {
                    "my-server": {
                        "task-1": {
                            "prompt": "XL prompt",
                        },
                        "xl-only": {
                            "cron": "0 10 * * *",
                            "prompt": "Only on XL",
                            "mode": "isolate",
                            "bot": "",
                            "ai_backend": "codex-cli",
                            "model": "gpt-5.4",
                            "enabled": True,
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_node_id(tmp_path, "my-server")
    args = _make_args(tmp_path, box_agent_dir=tmp_path)

    schedule_list(args)

    out = capsys.readouterr().out
    assert "task-1" in out
    assert "XL prompt" in out
    assert "xl-only" in out
    assert "node_overrides" not in out


# --- schedule_del ---


def test_del_removes_entry(tmp_path):
    _write_sched(tmp_path, "to-delete")
    args = _make_args(tmp_path, id="to-delete")
    schedule_del(args)
    path = tmp_path / "schedules.yaml"
    data = yaml.safe_load(path.read_text())
    assert "to-delete" not in (data or {})


def test_del_missing(tmp_path):
    args = _make_args(tmp_path, id="nonexistent")
    with pytest.raises(SystemExit):
        schedule_del(args)


# --- schedule_enable / schedule_disable ---


def test_enable(tmp_path):
    _write_sched(tmp_path, "my-task", enabled=False)
    args = _make_args(tmp_path, id="my-task")
    schedule_enable(args)
    assert _read_sched(tmp_path, "my-task")["enabled"] is True


def test_disable(tmp_path):
    _write_sched(tmp_path, "my-task", enabled=True)
    args = _make_args(tmp_path, id="my-task")
    schedule_disable(args)
    assert _read_sched(tmp_path, "my-task")["enabled"] is False


# --- schedule_show ---


def test_show_prints_content(tmp_path, capsys):
    _write_sched(tmp_path, "show-task", prompt="Hello world")
    args = _make_args(tmp_path, id="show-task")
    schedule_show(args)
    out = capsys.readouterr().out
    assert "show-task" in out
    assert "Hello world" in out


def test_show_applies_node_overrides_for_current_node(tmp_path, capsys):
    path = tmp_path / "schedules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "show-task": {
                    "cron": "0 9 * * *",
                    "prompt": "Base prompt",
                    "mode": "isolate",
                    "bot": "",
                    "ai_backend": "claude-cli",
                    "model": "sonnet",
                    "enabled": True,
                },
                "node_overrides": {
                    "my-server": {
                        "show-task": {
                            "prompt": "XL prompt",
                            "enabled": False,
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_node_id(tmp_path, "my-server")
    args = _make_args(tmp_path, id="show-task", box_agent_dir=tmp_path)

    schedule_show(args)

    out = capsys.readouterr().out
    assert "show-task" in out
    assert "XL prompt" in out
    assert "enabled: false" in out
    assert "node_overrides" not in out


def test_save_all_writes_multiline_prompt_with_block_scalar(tmp_path):
    path = tmp_path / "schedules.yaml"
    _save_all(path, {
        "show-task": {
            "cron": "0 9 * * *",
            "prompt": "Hello\nworld",
            "mode": "isolate",
            "bot": "",
                "enabled_on_nodes": "",
            "enabled": True,
        }
    })

    content = path.read_text()
    assert "prompt: |-" in content or "prompt: |" in content
    assert "  Hello" in content
    assert "  world" in content


def test_show_missing(tmp_path, capsys):
    args = _make_args(tmp_path, id="nonexistent")
    schedule_show(args)
    assert "not found" in capsys.readouterr().out


# --- schedule_run ---


def test_safe_print_falls_back_for_non_utf_console(monkeypatch):
    class FlakyStream:
        def __init__(self):
            self.encoding = "cp1252"
            self.calls = 0
            self.writes = []

        def write(self, text):
            self.calls += 1
            if self.calls == 1:
                raise UnicodeEncodeError("charmap", text, 0, 1, "boom")
            self.writes.append(text)
            return len(text)

        def flush(self):
            return None

    stream = FlakyStream()
    _safe_print("中文输出", file=stream)

    assert "".join(stream.writes) == "????\n"


def test_run_missing(tmp_path):
    args = _make_args(tmp_path, id="nonexistent", box_agent_dir=tmp_path, sync=False)
    with pytest.raises(SystemExit):
        schedule_run(args)


def test_run_calls_api(tmp_path, monkeypatch, capsys):
    """Default (async) mode sends async=True and prints 'triggered'."""
    from unittest.mock import MagicMock, patch
    import httpx

    _write_sched(tmp_path, "run-task", prompt="Say hello", mode="isolate")
    args = _make_args(tmp_path, id="run-task", box_agent_dir=tmp_path, sync=False)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True, "status": "scheduled"}
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    local_dir = tmp_path / "local"
    local_dir.mkdir(exist_ok=True)
    (local_dir / "api.sock").touch()

    with patch("httpx.Client", return_value=mock_client):
        schedule_run(args)

    mock_client.post.assert_called_once()
    call_json = mock_client.post.call_args[1]["json"]
    assert call_json["id"] == "run-task"
    assert call_json["async"] is True
    assert "triggered" in capsys.readouterr().out


def test_run_sync_calls_api(tmp_path, monkeypatch, capsys):
    """--sync mode waits for output and does not send async flag."""
    from unittest.mock import MagicMock, patch
    import httpx

    _write_sched(tmp_path, "run-task", prompt="Say hello", mode="isolate")
    args = _make_args(tmp_path, id="run-task", box_agent_dir=tmp_path, sync=True)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True, "output": "Hello!"}
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    local_dir = tmp_path / "local"
    local_dir.mkdir(exist_ok=True)
    (local_dir / "api.sock").touch()

    with patch("httpx.Client", return_value=mock_client):
        schedule_run(args)

    call_json = mock_client.post.call_args[1]["json"]
    assert call_json["id"] == "run-task"
    assert "async" not in call_json
    assert "Hello!" in capsys.readouterr().out


def test_run_calls_tcp_api_with_runtime_port_file(tmp_path):
    from unittest.mock import MagicMock, patch

    _write_sched(tmp_path, "run-task", prompt="Say hello", mode="isolate")
    args = _make_args(tmp_path, id="run-task", box_agent_dir=tmp_path, sync=False)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "api-port.txt").write_text("50762\n", encoding="utf-8")

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True, "status": "scheduled"}
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("httpx.Client", return_value=mock_client):
        schedule_run(args)

    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "http://127.0.0.1:50762/api/schedule/run"
    assert mock_client.post.call_args[1]["json"]["id"] == "run-task"


def test_run_api_connection_refused_errors(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    import httpx

    args = _make_args(tmp_path, id="run-task", box_agent_dir=tmp_path, sync=False)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("refused")

    local_dir = tmp_path / "local"
    local_dir.mkdir(exist_ok=True)
    (local_dir / "api.sock").touch()

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(SystemExit) as exc_info:
            schedule_run(args)
    assert exc_info.value.code == 1


def test_run_api_fallback_append_errors(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch
    import httpx

    _write_sched(tmp_path, "append-task", prompt="Hello bot", mode="append", bot="my-bot")
    args = _make_args(tmp_path, id="append-task", box_agent_dir=tmp_path, sync=False)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("refused")

    local_dir = tmp_path / "local"
    local_dir.mkdir(exist_ok=True)
    (local_dir / "api.sock").touch()

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(SystemExit):
            schedule_run(args)


# --- schedule_logs ---


def _write_run_log(tmp_path, task_id, records):
    """Write jsonl run log entries for a task."""
    import json

    runs_dir = tmp_path / "local" / "schedule-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{task_id}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _sample_log(task_id="test-task", time="2026-04-16T10:00:00", **overrides):
    record = {
        "time": time,
        "task": task_id,
        "mode": "isolate",
        "bot": "my-bot",
        "ai_backend": "claude-cli",
        "model": "sonnet",
        "workspace": "/tmp/ws",
        "prompt": "Say hello",
        "output": "Hello!",
        "error": "",
    }
    record.update(overrides)
    return record


def test_logs_empty(tmp_path, capsys):
    args = _make_args(tmp_path, id="", lines=20, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    assert "No schedule logs found" in capsys.readouterr().out


def test_logs_empty_for_task(tmp_path, capsys):
    args = _make_args(tmp_path, id="nonexistent", lines=20, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    assert "No logs found for 'nonexistent'" in capsys.readouterr().out


def test_logs_shows_entries(tmp_path, capsys):
    _write_run_log(tmp_path, "task-a", [
        _sample_log("task-a", "2026-04-16T10:00:00", output="Hello!"),
    ])
    args = _make_args(tmp_path, id="", lines=20, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    out = capsys.readouterr().out
    assert "task-a" in out
    assert "OK" in out
    assert "Hello!" in out


def test_logs_shows_error(tmp_path, capsys):
    _write_run_log(tmp_path, "task-a", [
        _sample_log("task-a", error="something broke", output=""),
    ])
    args = _make_args(tmp_path, id="", lines=20, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "something broke" in out


def test_logs_filter_by_id(tmp_path, capsys):
    _write_run_log(tmp_path, "task-a", [_sample_log("task-a")])
    _write_run_log(tmp_path, "task-b", [_sample_log("task-b")])
    args = _make_args(tmp_path, id="task-a", lines=20, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    out = capsys.readouterr().out
    assert "task-a" in out
    assert "task-b" not in out


def test_logs_limit(tmp_path, capsys):
    records = [_sample_log("task-a", f"2026-04-16T{10+i}:00:00") for i in range(5)]
    _write_run_log(tmp_path, "task-a", records)
    args = _make_args(tmp_path, id="", lines=2, output_json=False, box_agent_dir=tmp_path)
    schedule_logs(args)
    out = capsys.readouterr().out
    # Should only show 2 entries (most recent)
    assert out.count("task-a") == 2


def test_logs_json_output(tmp_path, capsys):
    import json

    _write_run_log(tmp_path, "task-a", [_sample_log("task-a")])
    args = _make_args(tmp_path, id="", lines=20, output_json=True, box_agent_dir=tmp_path)
    schedule_logs(args)
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["task"] == "task-a"


def test_logs_sorted_desc(tmp_path):
    _write_run_log(tmp_path, "task-a", [
        _sample_log("task-a", "2026-04-16T08:00:00"),
        _sample_log("task-a", "2026-04-16T12:00:00"),
        _sample_log("task-a", "2026-04-16T10:00:00"),
    ])
    local_dir = tmp_path / "local"
    entries = _load_run_logs(local_dir)
    assert entries[0]["time"] == "2026-04-16T12:00:00"
    assert entries[1]["time"] == "2026-04-16T10:00:00"
    assert entries[2]["time"] == "2026-04-16T08:00:00"


def test_logs_multiple_tasks_merged(tmp_path):
    _write_run_log(tmp_path, "task-a", [_sample_log("task-a", "2026-04-16T10:00:00")])
    _write_run_log(tmp_path, "task-b", [_sample_log("task-b", "2026-04-16T11:00:00")])
    local_dir = tmp_path / "local"
    entries = _load_run_logs(local_dir)
    assert len(entries) == 2
    assert entries[0]["task"] == "task-b"
    assert entries[1]["task"] == "task-a"
