"""Scheduler — cron-based task scheduling with isolate/append modes."""

import asyncio
import json
import logging
from copy import deepcopy
from contextlib import suppress
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from croniter import croniter

from boxagent.config import node_matches
from boxagent.utils import deep_merge_dicts
from boxagent.context import build_schedule_context

logger = logging.getLogger(__name__)

SCHEDULE_NODE_OVERRIDES_KEY = "node_overrides"


@dataclass
class ScheduleTask:
    """A scheduled task loaded from YAML."""

    id: str
    cron: str
    prompt: str
    mode: str = "isolate"  # "isolate" | "append"
    bot: str = ""
    ai_backend: str = ""
    model: str = ""
    yolo: bool = False
    enabled_on_nodes: str | list[str] = ""
    enabled: bool = True


def _validate_entry(task_id: str, raw: dict) -> ScheduleTask:
    """Validate a single schedule entry and return a ScheduleTask.

    Raises ValueError if required fields are missing or invalid.
    """
    cron_expr = raw.get("cron")
    if not cron_expr:
        raise ValueError(f"Schedule '{task_id}': missing required field 'cron'")
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"Schedule '{task_id}': invalid cron expression '{cron_expr}'")

    prompt = raw.get("prompt")
    if not prompt:
        raise ValueError(f"Schedule '{task_id}': missing required field 'prompt'")

    mode = raw.get("mode", "isolate")
    if mode not in ("isolate", "append"):
        raise ValueError(f"Schedule '{task_id}': invalid mode '{mode}', must be 'isolate' or 'append'")

    bot = raw.get("bot", "")
    if mode == "append" and not bot:
        raise ValueError(f"Schedule '{task_id}': 'bot' is required when mode=append")

    ai_backend = raw.get("ai_backend", "")
    if ai_backend and ai_backend not in ("claude-cli", "codex-cli", "codex-acp"):
        raise ValueError(
            f"Schedule '{task_id}': unknown ai_backend '{ai_backend}'"
        )

    model = raw.get("model", "")
    if mode == "isolate":
        if not ai_backend:
            raise ValueError(
                f"Schedule '{task_id}': 'ai_backend' is required when mode=isolate"
            )
        if not model:
            raise ValueError(
                f"Schedule '{task_id}': 'model' is required when mode=isolate"
            )

    return ScheduleTask(
        id=task_id,
        cron=cron_expr,
        prompt=prompt,
        mode=mode,
        bot=bot,
        ai_backend=ai_backend,
        model=model,
        yolo=bool(raw.get("yolo", False)),
        enabled_on_nodes=raw.get("enabled_on_nodes", ""),
        enabled=raw.get("enabled", True),
    )


def _load_schedule_yaml(path: Path) -> dict:
    """Load raw schedules YAML as a mapping."""
    if not path.is_file():
        return {}

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return {}

    return raw


def _base_schedule_entries(raw: dict) -> dict:
    """Return top-level schedule entries excluding reserved metadata blocks."""
    return {
        task_id: deepcopy(entry)
        for task_id, entry in raw.items()
        if task_id != SCHEDULE_NODE_OVERRIDES_KEY
    }


def load_schedule_entries(path: Path, node_id: str = "") -> dict[str, dict]:
    """Load raw schedule entries, applying node_overrides when requested."""
    raw = _load_schedule_yaml(path)
    if not raw:
        return {}

    entries = _base_schedule_entries(raw)
    if node_id:
        overrides = raw.get(SCHEDULE_NODE_OVERRIDES_KEY)
        if overrides is None:
            pass
        elif not isinstance(overrides, dict):
            logger.warning(
                "Ignoring schedules.%s: top-level %s must be a mapping",
                path.name,
                SCHEDULE_NODE_OVERRIDES_KEY,
            )
        else:
            node_override = overrides.get(node_id)
            if node_override is not None:
                if not isinstance(node_override, dict):
                    logger.warning(
                        "Ignoring schedules.%s %s.%s: override must be a mapping",
                        path.name,
                        SCHEDULE_NODE_OVERRIDES_KEY,
                        node_id,
                    )
                else:
                    entries = deep_merge_dicts(entries, node_override)

    filtered: dict[str, dict] = {}
    for task_id, entry in entries.items():
        if not isinstance(entry, dict):
            logger.warning("Skipping schedule '%s': entry is not a dict", task_id)
            continue
        filtered[task_id] = entry
    return filtered


def load_schedules(path: Path, node_id: str = "") -> dict[str, ScheduleTask]:
    """Load all schedules from YAML, applying node_overrides for node_id.

    Returns dict keyed by task id. Logs warnings for bad entries.
    """
    raw_entries = load_schedule_entries(path, node_id=node_id)

    tasks: dict[str, ScheduleTask] = {}
    for task_id, entry in raw_entries.items():
        try:
            tasks[task_id] = _validate_entry(task_id, entry)
        except Exception as e:
            logger.warning("Skipping schedule '%s': %s", task_id, e)
    return tasks


def compute_next_run(cron_expr: str, after: datetime) -> datetime:
    """Compute the next run time for a cron expression after a given datetime."""
    return croniter(cron_expr, after).get_next(datetime)


@dataclass
class BotRef:
    """Reference to a bot's CLIProcess and channel for scheduler use."""

    cli_process: object  # backend process
    channel: object  # TelegramChannel
    chat_id: str
    ai_backend: str = "claude-cli"
    telegram_token: str = ""


def _summarize_tool_calls(calls: list[str]) -> str:
    """Summarize tool calls like 'Bash×3, Read×2, Write×1'."""
    from collections import Counter
    counts = Counter(calls)
    return ", ".join(f"{name}×{cnt}" for name, cnt in counts.most_common())


@dataclass
class _SchedulerCallback:
    """Callback that collects agent output and sends result to Telegram."""

    channel: object | None
    chat_id: str
    task_id: str
    _text: str = ""
    _error: str = ""
    _tool_calls: list[str] = field(default_factory=list)

    async def on_stream(self, text: str) -> None:
        self._text += text

    async def on_tool_call(self, name: str, input: dict, result: str) -> None:
        self._tool_calls.append(name)

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: object = None,
        output: object = None,
    ) -> None:
        pass

    async def on_error(self, error: str) -> None:
        self._error = error

    async def on_file(self, path: str, caption: str = "") -> None:
        pass

    async def on_image(self, path: str, caption: str = "") -> None:
        pass

    async def send_result(self) -> None:
        """Send the collected result to the channel."""
        if self._error:
            msg = f"🤖 *{self.task_id}* Error: {self._error}"
        elif self._text.strip():
            msg = self._text.strip()
        else:
            msg = f"🤖 *{self.task_id}* (no output)"
        if self.channel is None:
            return
        await self.channel.send_text(self.chat_id, msg)


@dataclass
class Scheduler:
    """Async scheduler — wakes every 60s, loads YAML, fires matching cron jobs."""

    schedules_file: Path
    node_id: str = ""
    bot_refs: dict[str, BotRef] = field(default_factory=dict)
    telegram_bots: dict[str, str] = field(default_factory=dict)
    default_workspace: str = "."
    local_dir: str = ""
    copilot_api_port: int = 0
    max_catchup: int = 5  # max minutes to look back for missed runs
    _running: bool = field(default=False, repr=False)
    _executing: set[str] = field(default_factory=set, repr=False)
    _last_check: datetime | None = field(default=None, repr=False)

    async def run_forever(self) -> None:
        """Main loop: wake at the top of each minute, reload YAML, fire matching tasks."""
        self._running = True

        while self._running:
            # Sleep until the next minute boundary (:00)
            now = datetime.now()
            seconds_to_next_minute = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(seconds_to_next_minute)

            if not self._running:
                break

            now = datetime.now()
            tasks = load_schedules(self.schedules_file, node_id=self.node_id)

            # Build list of minutes to check: catch up missed ones + current
            check_times = self._minutes_to_check(now)

            for task in tasks.values():
                if not task.enabled:
                    continue
                if not node_matches(task.enabled_on_nodes, self.node_id):
                    continue
                if task.id in self._executing:
                    continue
                if any(croniter.match(task.cron, t) for t in check_times):
                    self._executing.add(task.id)
                    asyncio.create_task(self._fire(task))

            self._last_check = now

    def _minutes_to_check(self, now: datetime) -> list[datetime]:
        """Return the list of minute timestamps to check, including catch-up."""
        if self._last_check is None:
            return [now]

        gap = int((now - self._last_check).total_seconds() / 60)
        if gap <= 1:
            return [now]

        # Cap catch-up window
        gap = min(gap, self.max_catchup)
        minutes = []
        for i in range(gap, 0, -1):
            minutes.append(now - timedelta(minutes=i))
        minutes.append(now)
        return minutes

    async def _fire(self, task: ScheduleTask) -> None:
        """Execute a task and remove from executing set when done."""
        try:
            await self._run_task(task)
        except Exception as e:
            logger.error("Schedule '%s' failed: %s", task.id, e)
            env_info = self._format_env_info(task)
            await self._notify(task, f"🤖 *{task.id}* Error: {e}\n{env_info}")
        finally:
            self._executing.discard(task.id)

    async def execute_once(self, task: ScheduleTask) -> str:
        """Execute a task once and return output. Does not modify scheduler state."""
        return await self._run_task(task)

    async def _run_task(self, task: ScheduleTask) -> str:
        """Core execution logic shared by cron and manual triggers. Returns output text."""
        if task.mode == "isolate":
            return await self._execute_isolate(task)
        elif task.mode == "append":
            return await self._execute_append(task)
        else:
            raise ValueError(f"Unknown mode '{task.mode}'")

    def _build_prompt(self, task: ScheduleTask, *, effective_backend: str, effective_model: str) -> tuple[str, str]:
        """Build (append_system_prompt, user_prompt) for a scheduled task."""
        append_system_prompt = build_schedule_context(
            task_id=task.id,
            mode=task.mode,
            ai_backend=effective_backend,
            model=effective_model,
            workspace=self._get_workspace(task),
            node_id=self.node_id,
            bot=task.bot,
        )
        return append_system_prompt, task.prompt

    async def _execute_isolate(self, task: ScheduleTask) -> str:
        """Spawn an isolated backend process for the task."""
        logger.info("Schedule '%s' starting (isolate)", task.id)
        append_system_prompt, user_prompt = self._build_prompt(
            task,
            effective_backend=task.ai_backend,
            effective_model=task.model,
        )
        prompt = f"{append_system_prompt}\n{user_prompt}"
        t0 = datetime.now()
        try:
            text, callback = await self._spawn_isolate(task, user_prompt, append_system_prompt=append_system_prompt)
        except Exception as e:
            self._append_run_log(task, prompt=prompt, error=str(e))
            raise
        elapsed = datetime.now() - t0

        self._append_run_log(task, prompt=prompt, output=text)
        if task.bot:
            msg = self._format_isolate_notification(task, text, elapsed, callback)
            await self._notify(task, msg)

        logger.info("Schedule '%s' completed (isolate, %.1fs)", task.id, elapsed.total_seconds())
        return text

    def _format_isolate_notification(
        self,
        task: ScheduleTask,
        text: str,
        elapsed: timedelta,
        callback: "_SchedulerCallback",
    ) -> str:
        """Build the Telegram notification for an isolate task run."""
        secs = elapsed.total_seconds()
        if secs >= 60:
            elapsed_str = f"{int(secs // 60)}m{int(secs % 60)}s"
        else:
            elapsed_str = f"{secs:.1f}s"

        header = f"🤖【*Isolate*】{task.id}"
        meta_parts = [
            f"⏱ {elapsed_str}",
            f"📍 {self.node_id or '(unknown)'}",
            f"🧠 {task.ai_backend}/{task.model}",
        ]
        if callback._tool_calls:
            tool_summary = _summarize_tool_calls(callback._tool_calls)
            meta_parts.append(f"🔧 {tool_summary}")
        meta_line = "  |  ".join(meta_parts)

        if text:
            return f"{header}\n{meta_line}\n\n{text}"
        return f"{header} (no output)\n{meta_line}"

    async def _spawn_isolate(self, task: ScheduleTask, prompt: str, append_system_prompt: str = "") -> tuple[str, "_SchedulerCallback"]:
        """Spawn an isolated backend process and return (output_text, callback)."""
        backend = task.ai_backend
        workspace = self._get_workspace(task)
        callback = _SchedulerCallback(channel=None, chat_id="", task_id=task.id)

        if backend == "claude-cli":
            from boxagent.agent.claude_process import ClaudeProcess

            proc = ClaudeProcess(
                workspace=workspace,
                model=task.model,
                copilot_api_port=self.copilot_api_port,
                yolo=task.yolo,
            )
        elif backend == "codex-cli":
            from boxagent.agent.codex_process import CodexProcess

            proc = CodexProcess(
                workspace=workspace,
                model=task.model,
                copilot_api_port=self.copilot_api_port,
                yolo=task.yolo,
            )
        elif backend == "codex-acp":
            from boxagent.agent.acp_process import ACPProcess

            proc = ACPProcess(
                workspace=workspace,
                model=task.model,
                copilot_api_port=self.copilot_api_port,
            )
        else:
            raise ValueError(f"Schedule '{task.id}': unsupported ai_backend '{backend}'")

        proc.start()
        try:
            await proc.send(prompt, callback, model=task.model, append_system_prompt=append_system_prompt)
            if callback._error:
                raise RuntimeError(self._enrich_error(task, callback._error))
            return callback._text.strip(), callback
        finally:
            await proc.stop()

    def _resolve_isolate_bot_token(self, task: ScheduleTask) -> str:
        """Resolve isolate task.bot strictly via telegram_bots.yaml."""
        if not task.bot:
            return ""
        return self.telegram_bots.get(task.bot, "")

    def _find_active_bot_ref_by_token(self, token: str) -> BotRef | None:
        """Return the active bot ref using the given Telegram token, if any."""
        if not token:
            return None
        for ref in self.bot_refs.values():
            if ref.telegram_token == token:
                return ref
        return None

    def _resolve_unique_notify_chat_id(self) -> str:
        """Return the unique active chat id if there is exactly one."""
        chat_ids = {ref.chat_id for ref in self.bot_refs.values() if ref.chat_id}
        if len(chat_ids) == 1:
            return next(iter(chat_ids))
        return ""

    async def _notify_via_token(self, token: str, chat_id: str, msg: str) -> None:
        """Send a one-shot Telegram message directly via bot token."""
        from aiogram import Bot

        bot = Bot(token=token)
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=None)
        finally:
            with suppress(Exception):
                await bot.session.close()

    def _format_env_info(self, task: ScheduleTask) -> str:
        """Format node/environment info for error messages."""
        workspace = ""
        try:
            workspace = self._get_workspace(task)
        except Exception:
            pass
        parts = [
            f"node={self.node_id or '(unknown)'}",
            f"backend={task.ai_backend or '(inherit)'}",
            f"model={task.model or '(inherit)'}",
            f"mode={task.mode}",
            f"workspace={workspace or '(unknown)'}",
        ]
        return f"[{', '.join(parts)}]"

    def _enrich_error(self, task: ScheduleTask, error: str) -> str:
        """Prepend node/environment info to an error message."""
        return f"{error}\n{self._format_env_info(task)}"

    def _get_workspace(self, task: ScheduleTask) -> str:
        """Return the scheduler workspace for isolate tasks."""
        if not self.default_workspace:
            raise ValueError("Scheduler default_workspace is not configured")
        return self.default_workspace

    async def _execute_append(self, task: ScheduleTask) -> str:
        """Queue the prompt into a bot's primary CLIProcess."""
        ref = self.bot_refs.get(task.bot)
        if not ref:
            raise ValueError(f"Schedule '{task.id}': bot '{task.bot}' not found")

        logger.info("Schedule '%s' starting (append to %s)", task.id, task.bot)
        append_system_prompt, user_prompt = self._build_prompt(
            task,
            effective_backend="",
            effective_model="",
        )
        prompt = f"{append_system_prompt}\n{user_prompt}"
        await ref.channel.send_text(
            ref.chat_id,
            f"🤖【*Append*】*{task.id}*, prompt:\n\n{task.prompt}",
        )
        callback = _SchedulerCallback(
            channel=ref.channel, chat_id=ref.chat_id, task_id=task.id,
        )
        # append 模式始终沿用目标 bot 当前 backend/model/session；
        # task.ai_backend / task.model 仅供 isolate 模式使用，这里忽略。
        try:
            await ref.cli_process.send(user_prompt, callback, chat_id=ref.chat_id, append_system_prompt=append_system_prompt)
            await callback.send_result()
        except Exception as e:
            self._append_run_log(task, prompt=prompt, error=str(e))
            raise
        logger.info("Schedule '%s' completed (append)", task.id)
        if callback._error:
            self._append_run_log(task, prompt=prompt, error=callback._error)
            raise RuntimeError(self._enrich_error(task, callback._error))
        self._append_run_log(task, prompt=prompt, output=callback._text.strip())
        return callback._text.strip()

    async def _notify(self, task: ScheduleTask, msg: str) -> None:
        """Send an isolate schedule notification via a Telegram bot token."""
        if not task.bot:
            return

        token = self._resolve_isolate_bot_token(task)
        if not token:
            logger.warning(
                "Bot '%s' not found in telegram_bots.yaml for schedule '%s'",
                task.bot,
                task.id,
            )
            return

        ref = self._find_active_bot_ref_by_token(token)
        chat_id = ref.chat_id if ref and ref.chat_id else self._resolve_unique_notify_chat_id()
        if not chat_id:
            logger.warning(
                "Bot '%s' resolved to a token for schedule '%s', but no unique notify chat_id is available",
                task.bot,
                task.id,
            )
            return

        try:
            await self._notify_via_token(token, chat_id, msg)
        except Exception as e:
            logger.error(
                "Failed direct token notify for schedule '%s' via bot '%s': %s",
                task.id,
                task.bot,
                e,
            )

    def _append_run_log(self, task: ScheduleTask, *, prompt: str, output: str = "", error: str = "") -> None:
        """Append a scheduler run record to local/schedule-runs/<task>.jsonl."""
        if not self.local_dir:
            return
        path = Path(self.local_dir) / "schedule-runs" / f"{task.id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "task": task.id,
            "mode": task.mode,
            "bot": task.bot,
            "ai_backend": task.ai_backend,
            "model": task.model,
            "workspace": self._get_workspace(task),
            "prompt": prompt,
            "output": output,
            "error": error,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def stop(self) -> None:
        """Signal the scheduler loop to exit."""
        self._running = False
