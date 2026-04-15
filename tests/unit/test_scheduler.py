"""Tests for scheduler — YAML loading, cron, execution."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from boxagent.scheduler import (
    BotRef,
    Scheduler,
    ScheduleTask,
    _SchedulerCallback,
    _validate_entry,
    compute_next_run,
    load_schedules,
)


# --- helpers ---


def _write_schedules(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


def _make_scheduler(tmp_path, node_id="", bot_refs=None):
    sched_file = tmp_path / "schedules.yaml"
    return Scheduler(
        schedules_file=sched_file,
        node_id=node_id,
        bot_refs=bot_refs or {},
    )


# --- _validate_entry ---


def test_validate_valid_isolate():
    task = _validate_entry("test-task", {
        "cron": "0 9 * * *", "prompt": "Do something",
        "ai_backend": "claude-cli", "model": "sonnet",
    })
    assert task.id == "test-task"
    assert task.cron == "0 9 * * *"
    assert task.prompt == "Do something"
    assert task.mode == "isolate"
    assert task.bot == ""
    assert task.enabled is True


def test_validate_valid_append():
    task = _validate_entry("append-task", {
        "cron": "*/5 * * * *", "prompt": "Check status",
        "mode": "append", "bot": "ops-bot",
    })
    assert task.mode == "append"
    assert task.bot == "ops-bot"


def test_validate_all_fields():
    task = _validate_entry("full-task", {
        "cron": "30 8 * * 1-5", "prompt": "Weekly report",
        "mode": "isolate", "bot": "ops-bot",
        "ai_backend": "codex-acp", "model": "gpt-5.4",
        "enabled_on_nodes": "server-a", "enabled": False,
    })
    assert task.bot == "ops-bot"
    assert task.enabled_on_nodes == "server-a"
    assert task.enabled is False


def test_validate_missing_cron():
    with pytest.raises(ValueError, match="cron"):
        _validate_entry("test", {"prompt": "Do something"})


def test_validate_missing_prompt():
    with pytest.raises(ValueError, match="prompt"):
        _validate_entry("test", {"cron": "0 9 * * *"})


def test_validate_invalid_cron():
    with pytest.raises(ValueError, match="invalid cron"):
        _validate_entry("test", {"cron": "not-a-cron", "prompt": "Do it"})


def test_validate_invalid_mode():
    with pytest.raises(ValueError, match="invalid mode"):
        _validate_entry("test", {"cron": "0 9 * * *", "prompt": "Do it", "mode": "unknown"})


def test_validate_append_missing_bot():
    with pytest.raises(ValueError, match="bot.*required"):
        _validate_entry("test", {"cron": "0 9 * * *", "prompt": "Do it", "mode": "append"})


def test_validate_defaults():
    task = _validate_entry("minimal", {
        "cron": "0 * * * *", "prompt": "hi", "mode": "append", "bot": "my-bot"
    })
    assert task.mode == "append"
    assert task.enabled is True
    assert task.enabled_on_nodes == ""


def test_validate_enabled_on_nodes_string():
    task = _validate_entry("node-task", {
        "cron": "0 9 * * *", "prompt": "Do it",
        "ai_backend": "claude-cli", "model": "sonnet",
        "enabled_on_nodes": "cloud-pc",
    })
    assert task.enabled_on_nodes == "cloud-pc"


def test_validate_enabled_on_nodes_list():
    task = _validate_entry("node-task", {
        "cron": "0 9 * * *", "prompt": "Do it",
        "ai_backend": "claude-cli", "model": "sonnet",
        "enabled_on_nodes": ["cloud-pc", "home-server"],
    })
    assert task.enabled_on_nodes == ["cloud-pc", "home-server"]


def test_validate_ai_backend_and_model():
    task = _validate_entry("backend-task", {
        "cron": "0 9 * * *",
        "prompt": "Do it",
        "ai_backend": "codex-acp",
        "model": "gpt-5.4",
    })
    assert task.ai_backend == "codex-acp"
    assert task.model == "gpt-5.4"


def test_validate_isolate_requires_ai_backend():
    with pytest.raises(ValueError, match="ai_backend.*required"):
        _validate_entry("iso", {"cron": "0 9 * * *", "prompt": "Do it", "mode": "isolate", "model": "x"})


def test_validate_isolate_requires_model():
    with pytest.raises(ValueError, match="model.*required"):
        _validate_entry("iso", {"cron": "0 9 * * *", "prompt": "Do it", "mode": "isolate", "ai_backend": "claude-cli"})


def test_validate_invalid_ai_backend():
    with pytest.raises(ValueError, match="unknown ai_backend"):
        _validate_entry("bad-backend", {
            "cron": "0 9 * * *",
            "prompt": "Do it",
            "ai_backend": "not-real",
        })


def test_validate_yolo_default_false():
    task = _validate_entry("t", {
        "cron": "0 9 * * *", "prompt": "Do it",
        "ai_backend": "claude-cli", "model": "sonnet",
    })
    assert task.yolo is False


def test_validate_yolo_true():
    task = _validate_entry("t", {
        "cron": "0 9 * * *", "prompt": "Do it",
        "ai_backend": "claude-cli", "model": "sonnet",
        "yolo": True,
    })
    assert task.yolo is True


# --- load_schedules ---


def test_load_multiple_entries(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "task-0": {"cron": "0 9 * * *", "prompt": "Prompt 0", "ai_backend": "claude-cli", "model": "sonnet"},
        "task-1": {"cron": "0 10 * * *", "prompt": "Prompt 1", "ai_backend": "claude-cli", "model": "sonnet"},
        "task-2": {"cron": "0 11 * * *", "prompt": "Prompt 2", "ai_backend": "claude-cli", "model": "sonnet"},
    })
    tasks = load_schedules(path)
    assert len(tasks) == 3
    assert "task-0" in tasks
    assert "task-2" in tasks


def test_load_skips_invalid(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "good": {"cron": "0 9 * * *", "prompt": "Valid", "ai_backend": "claude-cli", "model": "sonnet"},
        "bad": {"cron": "not-valid"},  # missing prompt + bad cron
    })
    tasks = load_schedules(path)
    assert len(tasks) == 1
    assert "good" in tasks


def test_load_empty_file(tmp_path):
    path = tmp_path / "schedules.yaml"
    path.write_text("")
    tasks = load_schedules(path)
    assert tasks == {}


def test_load_nonexistent_file(tmp_path):
    tasks = load_schedules(tmp_path / "nonexistent.yaml")
    assert tasks == {}


def test_load_non_dict_entry(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "good": {"cron": "0 9 * * *", "prompt": "Valid", "ai_backend": "claude-cli", "model": "sonnet"},
        "bad": "not a dict",
    })
    tasks = load_schedules(path)
    assert len(tasks) == 1
    assert "good" in tasks


def test_load_applies_node_overrides_for_matching_node(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "daily-report": {
            "cron": "0 9 * * *",
            "prompt": "Base prompt",
            "ai_backend": "claude-cli",
            "model": "sonnet",
            "enabled": True,
        },
        "node_overrides": {
            "my-server": {
                "daily-report": {
                    "prompt": "XL prompt",
                    "enabled": False,
                },
                "xl-only": {
                    "cron": "0 10 * * *",
                    "prompt": "Only on XL",
                    "ai_backend": "codex-cli",
                    "model": "gpt-5.4",
                },
            }
        },
    })

    tasks = load_schedules(path, node_id="my-server")

    assert tasks["daily-report"].prompt == "XL prompt"
    assert tasks["daily-report"].enabled is False
    assert tasks["xl-only"].prompt == "Only on XL"
    assert tasks["xl-only"].ai_backend == "codex-cli"


def test_load_ignores_node_overrides_for_other_nodes(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "daily-report": {
            "cron": "0 9 * * *",
            "prompt": "Base prompt",
            "ai_backend": "claude-cli",
            "model": "sonnet",
        },
        "node_overrides": {
            "my-server": {
                "daily-report": {
                    "prompt": "XL prompt",
                },
            }
        },
    })

    tasks = load_schedules(path, node_id="macmini")

    assert tasks["daily-report"].prompt == "Base prompt"
    assert "node_overrides" not in tasks


def test_load_skips_invalid_node_overrides_shape(tmp_path):
    path = tmp_path / "schedules.yaml"
    _write_schedules(path, {
        "daily-report": {
            "cron": "0 9 * * *",
            "prompt": "Base prompt",
            "ai_backend": "claude-cli",
            "model": "sonnet",
        },
        "node_overrides": ["not-a-mapping"],
    })

    tasks = load_schedules(path, node_id="my-server")

    assert tasks["daily-report"].prompt == "Base prompt"


# --- compute_next_run ---


def test_next_run_same_day():
    after = datetime(2026, 3, 21, 8, 0, 0)
    nxt = compute_next_run("0 9 * * *", after)
    assert nxt == datetime(2026, 3, 21, 9, 0, 0)


def test_next_run_next_day():
    after = datetime(2026, 3, 21, 10, 0, 0)
    nxt = compute_next_run("0 9 * * *", after)
    assert nxt == datetime(2026, 3, 22, 9, 0, 0)


def test_next_run_every_5_min():
    after = datetime(2026, 3, 21, 8, 3, 0)
    nxt = compute_next_run("*/5 * * * *", after)
    assert nxt == datetime(2026, 3, 21, 8, 5, 0)


def test_next_run_weekday_only():
    # 2026-03-21 is Saturday
    after = datetime(2026, 3, 21, 10, 0, 0)
    nxt = compute_next_run("0 9 * * 1-5", after)
    # Next weekday is Monday 2026-03-23
    assert nxt == datetime(2026, 3, 23, 9, 0, 0)


# --- _SchedulerCallback ---


@pytest.fixture
def mock_channel():
    ch = AsyncMock()
    ch.send_text = AsyncMock()
    return ch


async def test_callback_collects_text(mock_channel):
    cb = _SchedulerCallback(channel=mock_channel, chat_id="123", task_id="t1")
    await cb.on_stream("Hello ")
    await cb.on_stream("world")
    assert cb._text == "Hello world"


async def test_callback_send_result_success(mock_channel):
    cb = _SchedulerCallback(channel=mock_channel, chat_id="123", task_id="t1")
    await cb.on_stream("Done!")
    await cb.send_result()
    mock_channel.send_text.assert_called_once_with("123", "Done!")


async def test_callback_send_result_error(mock_channel):
    cb = _SchedulerCallback(channel=mock_channel, chat_id="123", task_id="t1")
    await cb.on_error("something broke")
    await cb.send_result()
    mock_channel.send_text.assert_called_once_with("123", "🤖 *t1* Error: something broke")


async def test_callback_send_result_empty(mock_channel):
    cb = _SchedulerCallback(channel=mock_channel, chat_id="123", task_id="t1")
    await cb.send_result()
    mock_channel.send_text.assert_called_once_with("123", "🤖 *t1* (no output)")


async def test_callback_tool_call_noop(mock_channel):
    cb = _SchedulerCallback(channel=mock_channel, chat_id="123", task_id="t1")
    await cb.on_tool_call("Bash", {}, "")  # should not raise


# --- Scheduler._fire ---


async def test_fire_append_sends_to_cli(tmp_path):
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    task = ScheduleTask(
        id="t1", cron="0 9 * * *", prompt="Do something",
        mode="append", bot="my-bot",
    )
    sched._executing.add("t1")

    await sched._fire(task)

    mock_cli.send.assert_called_once()
    call_args = mock_cli.send.call_args
    # append_system_prompt now carries schedule context
    append_system_prompt = call_args.kwargs.get("append_system_prompt", "")
    assert "[BoxAgent Schedule]" in append_system_prompt
    assert "mode: append" in append_system_prompt
    assert "backend:" not in append_system_prompt
    assert "model:" not in append_system_prompt
    # user_prompt is just the task prompt
    assert call_args[0][0] == "Do something"
    assert "t1" not in sched._executing  # cleaned up
    # send_text called twice: task started notification + result
    assert mock_ch.send_text.call_count == 2
    first_call = mock_ch.send_text.call_args_list[0]
    assert "Append" in first_call[0][1]


async def test_fire_append_unknown_bot(tmp_path):
    sched = _make_scheduler(tmp_path)
    task = ScheduleTask(
        id="t1", cron="0 9 * * *", prompt="Do something",
        mode="append", bot="nonexistent",
    )
    sched._executing.add("t1")
    # Should not crash
    await sched._fire(task)
    assert "t1" not in sched._executing  # cleaned up even on error


# --- Scheduler.run_forever ---

# In these tests we patch asyncio.sleep to skip the wait-to-boundary,
# and patch croniter.match to control which tasks fire.


async def test_stop_exits_loop(tmp_path):
    sched = _make_scheduler(tmp_path)

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep):
        await sched.run_forever()

    assert not sched._running


async def test_run_forever_fires_matching_task(tmp_path):
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Fire now", "mode": "append", "bot": "my-bot"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    # Let created tasks run
    await asyncio.sleep(0.05)
    mock_cli.send.assert_called_once()


async def test_run_forever_skips_disabled(tmp_path):
    mock_cli = AsyncMock()
    mock_ch = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Disabled", "mode": "append", "bot": "my-bot", "enabled": False},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    mock_cli.send.assert_not_called()


async def test_run_forever_skips_non_matching_cron(tmp_path):
    mock_cli = AsyncMock()
    mock_ch = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "0 0 30 2 *", "prompt": "Never", "mode": "append", "bot": "my-bot"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=False):
        await sched.run_forever()

    mock_cli.send.assert_not_called()


async def test_run_forever_skips_wrong_node(tmp_path):
    mock_cli = AsyncMock()
    mock_ch = AsyncMock()

    sched = _make_scheduler(tmp_path, node_id="server-a", bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Wrong node", "mode": "append", "bot": "my-bot", "enabled_on_nodes": "server-b"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    mock_cli.send.assert_not_called()


async def test_run_forever_skips_wrong_node_filter(tmp_path):
    mock_cli = AsyncMock()
    mock_ch = AsyncMock()

    sched = _make_scheduler(tmp_path, node_id="home-server", bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Wrong node", "mode": "append", "bot": "my-bot", "enabled_on_nodes": "cloud-pc"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    mock_cli.send.assert_not_called()


async def test_run_forever_fires_matching_node(tmp_path):
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, node_id="cloud-pc", bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Right node", "mode": "append", "bot": "my-bot", "enabled_on_nodes": "cloud-pc"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    await asyncio.sleep(0.05)
    mock_cli.send.assert_called_once()


async def test_run_forever_skips_already_executing(tmp_path):
    """A task already in _executing should not be fired again."""
    mock_cli = AsyncMock()
    hang_event = asyncio.Event()

    async def _hang(*a, **kw):
        await hang_event.wait()

    mock_cli.send = AsyncMock(side_effect=_hang)
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Slow", "mode": "append", "bot": "my-bot"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    hang_event.set()
    await asyncio.sleep(0.05)

    # send() should have been called exactly once despite multiple ticks
    mock_cli.send.assert_called_once()


async def test_run_forever_hot_reload(tmp_path):
    """YAML changes are picked up on the next tick."""
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    # Start with no schedules
    _write_schedules(sched.schedules_file, {})

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Add a schedule after first tick
            _write_schedules(sched.schedules_file, {
                "t1": {"cron": "* * * * *", "prompt": "Added later", "mode": "append", "bot": "my-bot"},
            })
        if call_count >= 3:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep), \
         patch("boxagent.scheduler.croniter.match", return_value=True):
        await sched.run_forever()

    # Let created tasks run
    await asyncio.sleep(0.05)
    # Task was picked up on second tick
    mock_cli.send.assert_called_once()


# --- Scheduler._minutes_to_check (catch-up) ---


def test_minutes_to_check_no_last_check(tmp_path):
    sched = _make_scheduler(tmp_path)
    now = datetime(2026, 3, 21, 10, 0, 0)
    result = sched._minutes_to_check(now)
    assert result == [now]


def test_minutes_to_check_normal_gap(tmp_path):
    """Gap of 1 minute — just return now."""
    sched = _make_scheduler(tmp_path)
    sched._last_check = datetime(2026, 3, 21, 9, 59, 0)
    now = datetime(2026, 3, 21, 10, 0, 0)
    result = sched._minutes_to_check(now)
    assert result == [now]


def test_minutes_to_check_missed_3_minutes(tmp_path):
    """Gap of 3 minutes — catch up the 2 missed ones + now."""
    sched = _make_scheduler(tmp_path)
    sched._last_check = datetime(2026, 3, 21, 9, 57, 0)
    now = datetime(2026, 3, 21, 10, 0, 0)
    result = sched._minutes_to_check(now)
    assert len(result) == 4  # 9:58, 9:59, 10:00 ... wait
    # gap=3, so minutes: now-3, now-2, now-1, now
    assert result[0] == datetime(2026, 3, 21, 9, 57, 0)
    assert result[-1] == now


def test_minutes_to_check_capped_at_max(tmp_path):
    """Gap of 30 minutes — capped to max_catchup (5)."""
    sched = _make_scheduler(tmp_path)
    sched._last_check = datetime(2026, 3, 21, 9, 30, 0)
    now = datetime(2026, 3, 21, 10, 0, 0)
    result = sched._minutes_to_check(now)
    # capped to 5: now-5, now-4, now-3, now-2, now-1, now = 6 entries
    assert len(result) == 6
    assert result[0] == datetime(2026, 3, 21, 9, 55, 0)
    assert result[-1] == now


async def test_catchup_fires_missed_task(tmp_path):
    """If a task was missed during a long sleep, it fires on catch-up."""
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })
    # Simulate: last check was 3 minutes ago
    sched._last_check = datetime.now() - timedelta(minutes=3)

    # Task runs every minute
    _write_schedules(sched.schedules_file, {
        "t1": {"cron": "* * * * *", "prompt": "Catch me", "mode": "append", "bot": "my-bot"},
    })

    call_count = 0

    async def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sched.stop()

    with patch("boxagent.scheduler.asyncio.sleep", side_effect=fake_sleep):
        await sched.run_forever()

    await asyncio.sleep(0.05)
    # Should fire exactly once (deduped by _executing set)
    mock_cli.send.assert_called_once()


# --- Scheduler.execute_once ---


async def test_execute_once_append(tmp_path):
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123"),
    })

    task = ScheduleTask(
        id="t1", cron="0 9 * * *", prompt="Do something",
        mode="append", bot="my-bot",
    )
    result = await sched.execute_once(task)
    mock_cli.send.assert_called_once()
    # send_text called twice: task started notification + result
    assert mock_ch.send_text.call_count == 2
    assert "Append" in mock_ch.send_text.call_args_list[0][0][1]
    assert isinstance(result, str)


async def test_execute_once_append_unknown_bot(tmp_path):
    sched = _make_scheduler(tmp_path)
    task = ScheduleTask(
        id="t1", cron="0 9 * * *", prompt="Do something",
        mode="append", bot="nonexistent",
    )
    with pytest.raises(ValueError, match="not found"):
        await sched.execute_once(task)


async def test_execute_once_append_ignores_model_and_backend_fields(tmp_path):
    mock_cli = AsyncMock()
    mock_cli.send = AsyncMock()
    mock_ch = AsyncMock()
    mock_ch.send_text = AsyncMock()

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=mock_cli, channel=mock_ch, chat_id="123", ai_backend="claude-cli"),
    })

    task = ScheduleTask(
        id="t1", cron="0 9 * * *", prompt="Do something",
        mode="append", bot="my-bot", ai_backend="codex-cli", model="gpt-5.4-mini",
    )
    await sched.execute_once(task)
    call = mock_cli.send.call_args
    append_system_prompt = call.kwargs.get("append_system_prompt", "")
    assert "[BoxAgent Schedule]" in append_system_prompt
    assert call.args[0] == "Do something"
    assert "backend:" not in append_system_prompt
    assert "model:" not in append_system_prompt
    assert call.kwargs.get("chat_id") == "123"


async def test_execute_once_isolate_uses_requested_backend_and_model(tmp_path):
    captured = {}

    class FakeACP:
        def __init__(self, workspace, model="", copilot_api_port=0, **kwargs):
            captured["init"] = {
                "workspace": workspace,
                "model": model,
                "copilot_api_port": copilot_api_port,
            }
        def start(self):
            captured["started"] = True
        async def send(self, message, callback, model="", chat_id="", append_system_prompt=""):
            captured["send"] = {
                "message": message,
                "model": model,
                "chat_id": chat_id,
                "append_system_prompt": append_system_prompt,
            }
            await callback.on_stream("done")
        async def stop(self):
            captured["stopped"] = True

    sched = _make_scheduler(tmp_path, bot_refs={
        "my-bot": BotRef(cli_process=MagicMock(workspace="/tmp/work"), channel=AsyncMock(), chat_id="123", ai_backend="claude-cli"),
    })
    sched.default_workspace = "/ba/workspace"
    sched.copilot_api_port = 4141
    task = ScheduleTask(
        id="iso", cron="0 9 * * *", prompt="hello",
        ai_backend="codex-acp", model="gpt-5.4",
    )
    with patch("boxagent.agent.acp_process.ACPProcess", FakeACP):
        result = await sched.execute_once(task)

    assert result == "done"
    assert captured["init"] == {
        "workspace": "/ba/workspace",
        "model": "gpt-5.4",
        "copilot_api_port": 4141,
    }
    assert captured["send"]["model"] == "gpt-5.4"
    assert captured["send"]["chat_id"] == ""
    assert captured["send"]["message"] == "hello"
    assert "[BoxAgent Schedule]" in captured["send"]["append_system_prompt"]
    assert "backend: codex-acp" in captured["send"]["append_system_prompt"]
    assert captured["started"] is True
    assert captured["stopped"] is True


async def test_isolate_prefers_telegram_bots_mapping_over_bot_name(tmp_path):
    captured = {}

    class FakeClaude:
        def __init__(self, workspace, model="", copilot_api_port=0, **kwargs):
            captured["workspace"] = workspace
        def start(self):
            pass
        async def send(self, message, callback, model="", chat_id="", append_system_prompt=""):
            await callback.on_stream("ok")
        async def stop(self):
            pass

    mock_channel = AsyncMock()
    sched = _make_scheduler(tmp_path, bot_refs={
        "configured-bot": BotRef(
            cli_process=MagicMock(workspace="/tmp/work"),
            channel=mock_channel,
            chat_id="123",
            ai_backend="claude-cli",
            telegram_token="111:AAA",
        ),
    })
    sched.telegram_bots = {
        "my_test_bot": "111:AAA",
    }
    task = ScheduleTask(
        id="iso-map", cron="0 9 * * *", prompt="hello",
        mode="isolate", bot="my_test_bot", ai_backend="claude-cli", model="sonnet",
    )
    sched.default_workspace = "/ba/workspace"
    with patch("boxagent.agent.claude_process.ClaudeProcess", FakeClaude), \
         patch.object(sched, '_notify_via_token', AsyncMock()) as mock_notify:
        result = await sched.execute_once(task)

    assert result == "ok"
    assert captured["workspace"] == "/ba/workspace"
    mock_notify.assert_called_once()
    notified_msg = mock_notify.call_args[0][2]
    assert notified_msg.startswith("🤖【*Isolate*】iso-map\n")
    assert "claude-cli/sonnet" in notified_msg
    assert "ok" in notified_msg
    mock_channel.send_text.assert_not_called()


def test_build_prompt_injects_schedule_context(tmp_path):
    sched = _make_scheduler(tmp_path)
    sched.default_workspace = "/ba/workspace"
    sched.node_id = "my-canary"
    task = ScheduleTask(
        id="daily-sync",
        cron="0 9 * * *",
        prompt="Do work",
        mode="isolate",
        ai_backend="codex-acp",
        model="gpt-5.4",
        bot="my_test_bot",
    )

    append_system_prompt, user_prompt = sched._build_prompt(task, effective_backend="codex-acp", effective_model="gpt-5.4")

    assert "[BoxAgent Schedule]" in append_system_prompt
    assert "task: daily-sync" in append_system_prompt
    assert "mode: isolate" in append_system_prompt
    assert "node: my-canary" in append_system_prompt
    assert "backend: codex-acp" in append_system_prompt
    assert "model: gpt-5.4" in append_system_prompt
    assert "workspace: /ba/workspace" in append_system_prompt
    assert "bot: my_test_bot" in append_system_prompt
    assert user_prompt == "Do work"


async def test_notify_uses_direct_telegram_token_with_unique_chat_id(tmp_path):
    sched = _make_scheduler(tmp_path, bot_refs={
        "test-claude": BotRef(
            cli_process=MagicMock(),
            channel=AsyncMock(),
            chat_id="1777534489",
            ai_backend="claude-cli",
            telegram_token="111:AAA",
        ),
    })
    sched.telegram_bots = {"my_notification_bot": "222:BBB"}
    task = ScheduleTask(
        id="notify", cron="* * * * *", prompt="hello",
        mode="isolate", bot="my_notification_bot", ai_backend="claude-cli", model="sonnet",
    )

    calls = []
    async def fake_notify(token, chat_id, msg):
        calls.append((token, chat_id, msg))

    sched._notify_via_token = fake_notify
    await sched._notify(task, "hello world")

    assert calls == [("222:BBB", "1777534489", "hello world")]


async def test_notify_requires_bot_id_in_telegram_bots_yaml(tmp_path):
    sched = _make_scheduler(tmp_path, bot_refs={
        "test-claude": BotRef(
            cli_process=MagicMock(),
            channel=AsyncMock(),
            chat_id="1777534489",
            ai_backend="claude-cli",
            telegram_token="111:AAA",
        ),
    })
    task = ScheduleTask(
        id="notify", cron="* * * * *", prompt="hello",
        mode="isolate", bot="test-claude", ai_backend="claude-cli", model="sonnet",
    )

    with patch.object(sched, '_notify_via_token', AsyncMock()) as mock_notify:
        await sched._notify(task, "hello world")

    mock_notify.assert_not_called()


def test_resolve_unique_notify_chat_id_requires_single_chat(tmp_path):
    sched = _make_scheduler(tmp_path, bot_refs={
        "a": BotRef(cli_process=MagicMock(), channel=AsyncMock(), chat_id="1"),
        "b": BotRef(cli_process=MagicMock(), channel=AsyncMock(), chat_id="2"),
    })
    assert sched._resolve_unique_notify_chat_id() == ""


async def test_isolate_run_logs_output_to_local_dir(tmp_path):
    class FakeClaude:
        def __init__(self, workspace, model="", copilot_api_port=0, **kwargs):
            pass
        def start(self):
            pass
        async def send(self, message, callback, model="", chat_id="", append_system_prompt=""):
            await callback.on_stream("logged output")
        async def stop(self):
            pass

    local_dir = tmp_path / "local"
    sched = _make_scheduler(tmp_path)
    sched.default_workspace = "/ba/workspace"
    sched.local_dir = str(local_dir)
    task = ScheduleTask(
        id="echo-canary", cron="* * * * *", prompt="hello",
        mode="isolate", ai_backend="claude-cli", model="sonnet"
    )
    with patch("boxagent.agent.claude_process.ClaudeProcess", FakeClaude):
        result = await sched.execute_once(task)

    assert result == "logged output"
    log_path = local_dir / "schedule-runs" / "echo-canary.jsonl"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert 'logged output' in content
    assert '[BoxAgent Schedule]' in content


async def test_spawn_isolate_passes_yolo_to_claude(tmp_path):
    captured = {}

    class FakeClaude:
        def __init__(self, workspace, model="", copilot_api_port=0, **kwargs):
            captured["yolo"] = kwargs.get("yolo", False)
        def start(self):
            pass
        async def send(self, message, callback, model="", chat_id="", append_system_prompt=""):
            await callback.on_stream("ok")
        async def stop(self):
            pass

    sched = _make_scheduler(tmp_path)
    sched.default_workspace = "/ba/workspace"
    task = ScheduleTask(
        id="yolo-test", cron="* * * * *", prompt="hello",
        mode="isolate", ai_backend="claude-cli", model="sonnet", yolo=True,
    )
    with patch("boxagent.agent.claude_process.ClaudeProcess", FakeClaude):
        await sched.execute_once(task)

    assert captured["yolo"] is True


async def test_spawn_isolate_passes_yolo_to_codex(tmp_path):
    captured = {}

    class FakeCodex:
        def __init__(self, workspace, model="", copilot_api_port=0, **kwargs):
            captured["yolo"] = kwargs.get("yolo", False)
        def start(self):
            pass
        async def send(self, message, callback, model="", chat_id="", append_system_prompt=""):
            await callback.on_stream("ok")
        async def stop(self):
            pass

    sched = _make_scheduler(tmp_path)
    sched.default_workspace = "/ba/workspace"
    task = ScheduleTask(
        id="yolo-test", cron="* * * * *", prompt="hello",
        mode="isolate", ai_backend="codex-cli", model="gpt-5.4", yolo=True,
    )
    with patch("boxagent.agent.codex_process.CodexProcess", FakeCodex):
        await sched.execute_once(task)

    assert captured["yolo"] is True
