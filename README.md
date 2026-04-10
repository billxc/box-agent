# BoxAgent

BoxAgent, abbreviated as BA, is a self-hosted AI agent gateway: receive commands via Telegram, dispatch to Claude CLI or Codex ACP, and stream responses back.

## Documentation

- Backend setup overview: `docs/auth-api-keys.md`
- Claude setup: `docs/claude-setup.md`
- Codex setup: `docs/codex-setup.md`
- Maintainer-oriented codebase guide: `docs/codebase-guide.md`
- Product vision: `docs/vision.md`
- Design decisions log: `docs/decisions.md`

If you are trying to understand how the current implementation actually works, start with `docs/codebase-guide.md`.

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) package manager
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

### Install & Run

#### Option A: Run directly with uv (no clone needed)

```bash
# Run once from GitHub
uvx --from git+https://github.com/user/box-agent.git boxagent

# Or install as a tool for repeated use
uv tool install git+https://github.com/user/box-agent.git
boxagent doctor --fix
boxagent
```

#### Option B: Clone and develop locally

```bash
git clone https://github.com/user/box-agent.git && cd box-agent
uv sync --dev

# Check environment and auto-install missing dependencies
uv run boxagent doctor --fix

# Run
uv run boxagent
```

`doctor --fix` checks and installs: uv, Node.js, Claude CLI, Codex CLI, Codex ACP.

For backend auth / API keys, see `docs/auth-api-keys.md`.

#### Running as a Background Service

Use [easy-service](https://github.com/user/easy-service) to register BoxAgent as a system service:

```bash
# Install easy-service
uv tool install git+https://github.com/user/easy-service.git

# If installed as uv tool (Option A)
easy-service install boxagent -- boxagent
# If running from cloned repo (Option B)
easy-service install boxagent -- uv run --project /path/to/box-agent boxagent

easy-service start boxagent

# Other commands
easy-service status boxagent
easy-service stop boxagent
easy-service restart boxagent
easy-service uninstall boxagent
```

### Configure

Create `~/.boxagent/config.yaml`:

```yaml
global:
  log_level: info

bots:
  my-bot:
    ai_backend: claude-cli       # claude-cli | codex-cli | codex-acp
    model: opus                  # backend-specific model name, passed through as-is
    agent: ""                    # used by claude-cli; currently ignored by codex-acp
    workspace: ~/projects
    extra_skill_dirs:
      - ~/code/my-skills
    channels:
      telegram:
        token: "YOUR_BOT_TOKEN"           # or use bot_id (see below)
        allowed_users: [YOUR_TELEGRAM_USER_ID]
    display:
      tool_calls: summary   # silent | summary | detailed
```

Find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

#### Centralizing Bot Tokens (optional)

If you run multiple bots, you can put all tokens in `~/.boxagent/telegram_bots.yaml` and reference them by `bot_id` in `config.yaml`:

`telegram_bots.yaml`:
```yaml
"123456789": "123456789:AAA..."
"987654321": "987654321:BBB..."
```

`config.yaml`:
```yaml
bots:
  my-bot:
    channels:
      telegram:
        bot_id: "123456789"          # looked up from telegram_bots.yaml
        allowed_users: [YOUR_ID]
```

Resolution priority: `token` (direct) > `bot_id` (lookup from `telegram_bots.yaml`) > error. Both formats are fully backward compatible.

#### Node Filtering (optional)

When multiple machines share the same config directory, you can restrict which bots and scheduled tasks run on each machine.

Create `~/.boxagent/local/local.yaml` (machine-local, not shared):
```yaml
node_id: cloud-pc
```

Then in `config.yaml`, set `enabled_on_nodes` on any bot (or scheduled task):
```yaml
bots:
  claude:
    enabled_on_nodes: "cloud-pc"     # only runs on cloud-pc
    ...
  codex:
    # no enabled_on_nodes = runs everywhere
    ...
```

`enabled_on_nodes` accepts a string or a list of strings. Omitting it means "run on all nodes".

> **Migration note:** If you still have `global.node_id` in `config.yaml`, it will be used as a fallback (with a deprecation warning). Move it to `local.yaml` as `node_id` at your earliest convenience.

To run an isolated BA instance on the same machine, set `BOX_AGENT_DIR` or pass `--box-agent-dir` / `--ba-dir`.
BA uses that directory itself as the config directory, and uses the sibling `<BOX_AGENT_DIR>-local` directory for runtime state such as sessions and `api.sock`.

### Run

```bash
# If installed as uv tool
boxagent

# If running from cloned repo
uv run boxagent
```

Example for a separate test instance:

```bash
BOX_AGENT_DIR=/path/to/ba-test-dir boxagent
BOX_AGENT_DIR=/path/to/ba-test-dir uv run boxagent
boxagent --ba-dir /path/to/ba-test-dir
```

The bot connects to Telegram and starts listening. Send it a message from your allowed Telegram account.

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | List available commands |
| `/status` | Show bot state, session ID, uptime |
| `/new` | Start a fresh conversation (clear session) |
| `/compact` | Summarize conversation and start new session with context |
| `/model` | Show or switch model (e.g. `/model sonnet`) |
| `/cancel` | Cancel the current running task |
| `/resume` | List and resume a previous session |
| `/exec` | Execute a shell command (e.g. `/exec ls -la`, `/exec -t 60 make build`) |
| `/verbose` | Cycle tool call display (silent/summary/detailed) |
| `/sync_skills` | Re-sync linked skill directories |
| `/version` | Show BoxAgent version |

Any other text message is sent to the configured backend as a prompt. Photos and documents are downloaded to temporary files and included as local file paths. Prefix with `@model` (e.g. `@opus explain this`) to use a specific model for one message.

## Backends

| `ai_backend` | Runtime | Session Continuity | Restart Behavior | Notes |
|--------------|---------|--------------------|------------------|-------|
| `claude-cli` | Spawns `claude` per turn with `--resume` | Persists across turns and gateway restarts | Restored from `sessions.yaml` | `agent` is passed through as `--agent` |
| `codex-cli` | Spawns `codex exec` per turn with `--json` | Persists via `thread_id` across turns and restarts | Restored from `sessions.yaml`; resume via `codex exec resume <thread_id>` | Uses JSONL output parsing; `--dangerously-bypass-approvals-and-sandbox` for non-interactive use |
| `codex-acp` | Keeps an ACP connection to `codex-acp` and sends `session/prompt` turns | Native continuity while the same ACP connection stays alive | Restores the saved ACP session across gateway restart via `load_session(session_id, cwd)` when possible; `/cancel`, `/new`, or `/compact` still reset into a fresh ACP session | `agent` is currently ignored; skills sync to `{workspace}/.agents/skills/` |

## Scheduled Tasks

Cron-based task scheduling. The scheduler wakes at each minute boundary, loads `~/.boxagent/schedules.yaml`, and fires matching tasks.

### Schedule File

`~/.boxagent/schedules.yaml`:

```yaml
daily-report:
  cron: "0 9 * * *"
  prompt: "Check disk usage and summarize"
  mode: isolate
  ai_backend: codex-acp
  model: gpt-5.4
  bot: my-bot
  enabled_on_nodes: ""
  enabled: true

check-updates:
  cron: "0 */2 * * *"
  prompt: "Check for dependency updates"
  mode: append
  bot: my-bot
  enabled: true

node_overrides:
  my-server:
    daily-report:
      prompt: "Check disk usage and summarize"
    xl-only-task:
      cron: "30 9 * * *"
      prompt: "Run only on XL"
      mode: isolate
      ai_backend: codex-cli
      model: gpt-5.4
      enabled: true
```

`node_overrides` works the same way as in `config.yaml`: when the current `node_id` matches, BoxAgent deep-merges the override block into the base schedule definitions. You can override an existing task or add a node-only task.

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `cron` | yes | — | 5-field cron expression |
| `prompt` | yes | — | Prompt to send to the selected backend |
| `mode` | no | `isolate` | `isolate` = spawn new process; `append` = send to bot's existing session |
| `bot` | append only | `""` | Bot selector. In `append` mode it must be the configured bot name. In `isolate` mode it is treated as a Telegram bot id/name from `telegram_bots.yaml`; it is no longer resolved by configured bot name. This affects bot/channel resolution only; isolate workspace still defaults to `<ba-dir>/workspace`. |
| `ai_backend` | no | inherited / `claude-cli` | Backend override for the schedule. Optional; only used by `isolate` mode (`claude-cli` / `codex-cli` / `codex-acp`) |
| `model` | no | `""` | Per-task model override. Optional; only used by `isolate` mode |
| `enabled_on_nodes` | no | `""` | Only run on matching nodes (matches `node_id` from `local.yaml`); accepts string or list |
| `enabled` | no | `true` | Enable/disable without deleting |

Reserved key: top-level `node_overrides` is metadata, not a task id.

### Execution Modes

- **isolate**: Runs the task in a fresh backend invocation. If `ai_backend` is set, it wins. Otherwise BoxAgent inherits the referenced bot backend when `bot` is set, and falls back to `claude-cli` when no bot is provided. `model` is passed through to the isolated backend.
- **append**: Queues the prompt into a bot's existing primary session. Shares that bot's current conversation context and always uses that bot's configured backend/model/session. `ai_backend` and `model` are optional fields but ignored in append mode.

### CLI Management

```bash
# Add a schedule
boxagent schedule add --id daily-report --cron "0 9 * * *" --prompt "Check disk usage" --mode isolate --ai-backend claude-cli --model sonnet

# List all schedules
boxagent schedule list

# Show details
boxagent schedule show --id daily-report

# Enable / disable
boxagent schedule enable --id daily-report
boxagent schedule disable --id daily-report

# Delete
boxagent schedule del --id daily-report

# Run once immediately (requires gateway running)
boxagent schedule run --id daily-report
```

`schedule list` and `schedule show` display the effective schedule set for the current node after applying `node_overrides`. `schedule add` / `del` / `enable` / `disable` edit only the base task definitions; edit `schedules.yaml` directly for node-specific override blocks.

### Catch-up

If the scheduler loop was blocked while the process stayed alive, it compensates for missed runs by checking up to 5 past minutes on wake. This catch-up window is memory-only and does not survive a full gateway restart.

### Media Tools (MCP)

Interactive Telegram turns, and append-mode scheduled tasks that run through a Telegram-backed bot session, get built-in MCP tools for sending media back to the same chat:

| Tool | Description |
|------|-------------|
| `send_photo` | Send an image (jpg, png, etc.) |
| `send_document` | Send a file/document |
| `send_video` | Send a video (mp4, etc.) |
| `send_audio` | Send an audio file (mp3, ogg, etc.) |
| `send_animation` | Send a GIF animation |

These tools are injected automatically when a chat-backed turn is running. Isolate schedules do not currently receive Telegram media MCP injection.

## Architecture

```
Telegram
  ↕
TelegramChannel
  ↕
Router
  ├─ ClaudeProcess     ─ claude CLI
  ├─ CodexProcess      ─ codex CLI (exec --json)
  └─ ACPProcess        ─ codex-acp
  ↕
Storage / Watchdog / Scheduler / HTTP API
```

- **ClaudeProcess**: Spawns `claude --output-format stream-json -p <msg>` per turn. Parses NDJSON output and maintains session continuity via `--resume`. Inherits from `BaseCLIProcess`.
- **CodexProcess**: Spawns `codex exec --json <msg>` per turn. Parses JSONL output (thread.started, item.completed, etc.) and maintains session continuity via `codex exec resume <thread_id>`. Inherits from `BaseCLIProcess`.
- **ACPProcess**: Maintains an ACP connection to `codex-acp`, maps `session_update` events to `on_stream()` / `on_tool_update()`, and uses `session/cancel` for in-flight turn cancellation.
- **TelegramChannel**: Sends/receives via aiogram 3. Streams responses by editing messages, throttled at 300ms / 200 chars. Uses MarkdownV2 formatting with a single-pass tokenizer (`mdv2.py`).
- **Router**: Auth check → command dispatch → agent dispatch. Adapts AgentCallback to channel output.
- **Gateway**: Orchestrates all components. Starts/stops bots, wires Storage + Watchdog.
- **Storage**: Persists session IDs to `~/.boxagent/local/sessions.yaml`. Restart resume is native for `claude-cli`, and `codex-acp` now also attempts native recovery via `load_session(session_id, cwd)`.
- **Watchdog**: Checks backend state every 30s and recreates a backend when it enters `dead`.
- **Scheduler**: Wakes at minute boundaries, loads `schedules.yaml`, fires matching cron tasks, and supports isolate/append modes with in-process catch-up for missed runs.

## Configuration

### Environment Overrides

The current implementation supports these overrides:

```bash
BOXAGENT_MY_BOT_workspace=/data/projects
BOXAGENT_GLOBAL_LOG_LEVEL=debug
BOXAGENT_GLOBAL_API_PORT=8888
```

Per-bot override keys follow `BOXAGENT_<UPPER_BOT_NAME>_<KEY>` (hyphens → underscores), but today only `workspace` is wired up in code.

### CLI Options

```bash
uv run boxagent --config /path/to/config/dir
```

## Development

### Run Tests

```bash
# Unit tests only (default)
uv run pytest

# With verbose output
uv run pytest -v

# Integration tests (currently exercise the Claude CLI path)
uv run pytest -m integration

# E2E tests (requires bot token + chat ID)
BOXAGENT_TEST_BOT_TOKEN="..." BOXAGENT_TEST_CHAT_ID="..." uv run pytest -m integration
```

### Project Structure

```
src/boxagent/
├── main.py              # Entry point, CLI args, signal handling
├── config.py            # YAML config loading + validation
├── gateway.py           # Component orchestrator
├── router.py            # Auth, commands, dispatch + ChannelCallback
├── storage.py           # Session + PID persistence
├── watchdog.py          # Process liveness monitor
├── scheduler.py         # Cron-based task scheduler
├── schedule_cli.py      # CLI subcommands for schedule management
├── mcp_server.py        # Telegram media MCP server
├── agent/
│   ├── callback.py          # AgentCallback protocol
│   ├── base_cli.py          # Shared subprocess-per-turn base class
│   ├── claude_process.py    # Claude CLI backend
│   ├── codex_process.py     # Codex CLI backend
│   └── acp_process.py       # ACP session bridge for codex-acp
└── channels/
    ├── base.py          # Channel protocol + data types
    ├── mdv2.py          # Markdown → Telegram MarkdownV2 converter
    ├── splitter.py      # Message splitting (4096 char limit)
    └── telegram.py      # Telegram bot via aiogram 3
```

### Test Structure

```
tests/
├── unit/
│   ├── test_claude_process.py     # ClaudeProcess stream parsing, cancel, queue
│   ├── test_codex_process.py      # CodexProcess JSONL parsing, resume, cancel
│   ├── test_acp_process.py        # ACPProcess event mapping, cancel, lifecycle
│   ├── test_base_cli.py           # BaseCLIProcess command shim resolution
│   ├── test_config.py             # Config loading, validation, env overrides
│   ├── test_context.py            # Session context building and field injection
│   ├── test_router.py             # Auth, commands, dispatch
│   ├── test_router_cancel_integration.py  # Router-level /cancel with backend state
│   ├── test_router_late_stream_race.py    # Late stream chunks after router close
│   ├── test_commands.py           # /status, /new, /cancel, /compact, /model, /exec
│   ├── test_gateway.py            # Start/stop orchestration
│   ├── test_storage.py            # Session persistence helpers
│   ├── test_watchdog.py           # Dead process detection
│   ├── test_splitter.py           # Message splitting logic
│   ├── test_mdv2.py               # MarkdownV2 conversion
│   ├── test_telegram_channel.py   # TelegramChannel send/stream/throttle
│   ├── test_typing_indicator.py   # Typing indicator lifecycle management
│   ├── test_display.py            # Tool call formatting modes
│   ├── test_scheduler.py          # Cron loading, catch-up, append/isolate flows
│   ├── test_schedule_cli.py       # Schedule CLI subcommands (add, del, enable, list)
│   ├── test_mcp_server.py         # MCP server tools and media sending
│   ├── test_harness_judge.py      # Rule-based harness result judging
│   └── test_main.py               # CLI entry point, --ba-dir flag
└── integration/
    ├── test_cli_real.py           # Real Claude CLI subprocess path
    └── test_e2e.py                # Real Telegram + gateway flow
```
