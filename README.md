# BoxAgent

BoxAgent (BA) is a **Personal Agent Network**: one user, multiple machines, multiple AI agents, collaborating with each other and reachable from your phone or browser.

A single user. Many machines (laptop, desktop, dev box) joined into one network. Many AI agents (Claude CLI, Codex) on each machine, with admins delegating tasks to specialists. One coherent control plane via Telegram, Web, iOS, or MCP. No multi-tenant, no SaaS, no agent logic of its own — BoxAgent only orchestrates and bridges; the agents themselves are Claude / Codex / etc.

Single-machine, single-agent is just the smallest deployment shape; the full product is distributed and multi-agent by design.

## Documentation

- Vision and scope: `docs/vision.md`
- Maintainer-oriented codebase guide: `docs/codebase-guide.md` ← start here for "how does this code work"
- Design decisions log: `docs/decisions.md`
- Backend setup: `docs/auth-api-keys.md`, `docs/claude-setup.md`, `docs/codex-setup.md`
- Workgroup (multi-agent) design: `docs/workgroup-design.md`

## Architecture

```
            Telegram      Web UI       iOS app       MCP
                ↘            ↓            ↓           ↙
                          ┌──────────────────┐
                          │   Transports     │   external interaction
                          └────────┬─────────┘
                                   ↓
                          ┌──────────────────┐
                          │   Router         │   per-bot session control
                          │   (auth, /-cmds, │
                          │    dispatch)     │
                          └────────┬─────────┘
                                   ↓
                          ┌──────────────────┐
                          │   Backend CLI    │   Claude / Codex
                          │   (subprocess)   │
                          └──────────────────┘

           ── one machine running one or more bots ──

         ╔════════════════════════════════════════════╗
         ║  Cluster                                   ║
         ║   Multiple machines joined via devtunnel.  ║
         ║   Host's web UI federates every node's     ║
         ║   bots; remote bots are HTTP/SSE-proxied.  ║
         ╠════════════════════════════════════════════╣
         ║  Workgroup                                 ║
         ║   One admin agent + N specialist agents.   ║
         ║   Admin delegates via MCP; specialists can ║
         ║   live on any cluster node.                ║
         ╚════════════════════════════════════════════╝
```

Cluster and Workgroup are first-class capabilities — they're what makes BoxAgent a *network* of agents instead of a single bot. The single-machine / single-bot setup at the top of the diagram is the smallest valid deployment, not the default goal.

Internally the code is layered so that the Core boxes (Transports / Router / Backend / Sessions / Scheduler / Watchdog / Gateway) do not depend on `cluster/` or `workgroup/`; the dependency direction is one-way. See `docs/codebase-guide.md`.

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) package manager
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) — or skip Telegram and use Web UI only

### Install & Run

#### Option A: Run directly with uv (no clone needed)

```bash
# Run once from GitHub
uvx --from git+https://github.com/billxc/box-agent.git boxagent

# Or install as a tool for repeated use
uv tool install git+https://github.com/billxc/box-agent.git
boxagent doctor --fix
boxagent
```

#### Option B: Clone and develop locally

```bash
git clone https://github.com/billxc/box-agent.git && cd box-agent
uv sync --dev

# Check environment and auto-install missing dependencies
uv run boxagent doctor --fix

# Run
uv run boxagent
```

`doctor --fix` checks and installs: uv, Node.js, Claude CLI, Codex CLI.

For backend auth / API keys, see `docs/auth-api-keys.md`.

#### Running as a Background Service

Use [easy-service](https://github.com/billxc/easy-service) to register BoxAgent as a system service:

```bash
uv tool install git+https://github.com/billxc/easy-service.git

# If installed as uv tool (Option A)
easy-service install boxagent -- boxagent
# If running from cloned repo (Option B)
easy-service install boxagent -- uv run --project /path/to/box-agent boxagent
# If running via uvx (no install needed)
easy-service install boxagent -- uvx --from git+https://github.com/billxc/box-agent.git boxagent

easy-service start boxagent

# Other commands
easy-service status boxagent
easy-service stop boxagent
easy-service restart boxagent
easy-service uninstall boxagent
```

## Configure

Create `~/.boxagent/config.yaml`:

```yaml
global:
  log_level: info

bots:
  my-bot:
    ai_backend: claude-cli       # claude-cli | codex-cli | agent-sdk-claude | agent-sdk-copilot
    model: opus                  # backend-specific model name, passed through as-is
    agent: ""                    # used by claude-cli; ignored by codex-cli
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

### Centralizing Bot Tokens (optional)

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

### Node Filtering (optional)

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

## Channels

BoxAgent reaches you over multiple channels at once; pick whichever you need.

### Telegram

Primary channel — best streaming, media handling, and mobile experience. Configure under `bots.<name>.channels.telegram` as shown above.

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
| `/sessions` | Browse all sessions with search and filters (e.g. `/sessions chromium 7d backend:codex-cli p2`) |
| `/exec` | Execute a shell command (e.g. `/exec ls -la`, `/exec -t 60 make build`) |
| `/verbose` | Cycle tool call display (silent/summary/detailed) |
| `/sync_skills` | Re-sync linked skill directories |
| `/version` | Show BoxAgent version |

Any other text message is sent to the configured backend as a prompt. Photos and documents are downloaded to temporary files and included as local file paths. Prefix with `@model` (e.g. `@opus explain this`) to use a specific model for one message.

### Web UI

Every BoxAgent process exposes a browser chat at `http://127.0.0.1:9292/` by default. Vanilla HTML/CSS/JS, mobile-first, dark/light auto.

- Lists every web-enabled bot/workgroup admin in a sidebar.
- Per-chat sessions (telegram chat id, web uuid) are surfaced together; click a session to load its full transcript (chained across `/compact` boundaries).
- Streaming output renders incrementally as the backend produces tokens.
- Dedicated "Resume Claude session" picker: lists every session in `~/.claude/projects/*` grouped by project, picks a session, resumes via `claude --resume` while routing the chat through the original cwd.

Token-gated for non-localhost access:

```yaml
# config.yaml
global:
  web_token: "shared-secret"        # required for remote / tunnel access
  web_port: 9292                    # configurable; default 9292
  web_host: "127.0.0.1"             # 0.0.0.0 to expose to LAN
```

Then visit `http://<host>:9292/?token=<shared-secret>` once — the token is cached in localStorage.

### iOS app

Native SwiftUI client at `ios/BoxAgent/`. Talks to the Web UI's HTTP API (`/api/send`, `/api/stream`), reusing the same SSE event format as the browser. Useful when you want a native iOS experience instead of the mobile web view.

Open `ios/BoxAgent/BoxAgent.xcodeproj` in Xcode, copy `Local.xcconfig.example` → `Local.xcconfig`, and set the server URL + `web_token`. Build to your iPhone or simulator.

### MCP

The agents themselves get a built-in MCP server exposing BoxAgent tools (send media, manage schedules, send messages between agents, browse sessions). This is what lets, e.g., a Claude session running inside BoxAgent send you a photo back to the same Telegram chat.

| Tool | Description |
|------|-------------|
| `send_photo` | Send an image (jpg, png, etc.) |
| `send_document` | Send a file/document |
| `send_video` | Send a video (mp4, etc.) |
| `send_audio` | Send an audio file (mp3, ogg, etc.) |
| `send_animation` | Send a GIF animation |
| `sessions_list` | Browse unified sessions with search and filters |
| `send_to_agent` | Workgroup admin → specialist task delegation |
| `send_to_peer` | Workgroup admin → another admin (cross-node OK) |

These tools are injected automatically when a chat-backed turn is running. Isolate schedules do not currently receive Telegram media MCP injection.

## Backends

| `ai_backend` | Runtime | Session Continuity | Restart Behavior | Notes |
|--------------|---------|--------------------|------------------|-------|
| `claude-cli` | Spawns `claude` per turn with `--resume` | Persists across turns and gateway restarts | Restored from `sessions.yaml` | `agent` is passed through as `--agent` |
| `codex-cli` | Spawns `codex exec` per turn with `--json` | Persists via `thread_id` across turns and restarts | Restored from `sessions.yaml`; resume via `codex exec resume <thread_id>` | Uses JSONL output parsing; `--dangerously-bypass-approvals-and-sandbox` for non-interactive use |
| `agent-sdk-claude` | Calls `claude_agent_sdk.query()` in-process per turn | Persists via SDK's `resume` option | Restored from `sessions.yaml`; passes `session_id` to `ClaudeAgentOptions.resume` | Typed message stream (no NDJSON parsing); `yolo` maps to `permission_mode="bypassPermissions"`. Same `claude` CLI under the hood, but the SDK manages it. |
| `agent-sdk-copilot` | Maintains a long-lived `CopilotClient` (subprocess to GitHub Copilot CLI) and a per-bot `CopilotSession` | Persists via `client.resume_session(session_id)` | Restored from `sessions.yaml` | `yolo` maps to `PermissionHandler.approve_all`; non-yolo currently denies all tool calls (interactive approval not yet wired). `agent` is ignored. |

## Cluster (multi-machine)

For driving multiple machines from a single browser. One node is the **host** (auto-creates and hosts a [devtunnel](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/)); other nodes are **guests** that dial the host over WebSocket.

Add to the shared `config.yaml`:

```yaml
cluster:
  host: mbp                         # node_id of the host machine
  tunnel_name: boxagent-cluster     # devtunnel name to manage
  token: "<shared-cluster-secret>"
```

Each machine reads the same file; whichever has `node_id == cluster.host` becomes host, the rest auto-dial. The host's `/api/bots` then federates every connected guest's bots, and selecting a remote bot in the web UI proxies HTTP/SSE through the WebSocket transparently.

**Three layers of auth**:

1. Devtunnel JWT — only the same Microsoft account can mint a connect token (guests use the locally-logged-in `devtunnel` CLI on demand).
2. `cluster.token` — required in the guest's hello frame; gates membership.
3. `web_token` — gates browser/RPC HTTP calls.

Guests do not need their own public exposure (NAT-friendly outbound WS).

## Workgroup (multi-agent)

A workgroup is one **admin** agent plus zero or more **specialist** agents. The user only talks to the admin; the admin delegates tasks to specialists via the `send_to_agent` MCP tool, specialists return results, the admin replies. Specialists can live on the same machine or on another cluster node.

Add to `config.yaml` alongside `bots:`:

```yaml
workgroups:
  my-team:
    ai_backend: claude-cli
    model: opus
    admin_workspace: ~/projects/team
    display_name: "My Team"
    channels:
      telegram:
        bot_id: "123456789"
        allowed_users: [YOUR_ID]
    specialists:
      reviewer:
        ai_backend: claude-cli
        workspace: ~/projects/team/review
      researcher:
        ai_backend: codex-cli
        workspace: ~/projects/team/research
```

Cross-admin messaging (admin-to-admin, possibly across cluster nodes) uses `send_to_peer`. See `docs/workgroup-design.md` for full details.

## Scheduled Tasks

Cron-based task scheduling. The scheduler wakes at each minute boundary, loads `~/.boxagent/schedules.yaml`, and fires matching tasks.

### Schedule File

`~/.boxagent/schedules.yaml`:

```yaml
daily-report:
  cron: "0 9 * * *"
  prompt: "Check disk usage and summarize"
  mode: isolate
  ai_backend: codex-cli
  model: gpt-5.4
  timeout_seconds: 1800
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
| `ai_backend` | isolate yes | `""` | Backend for isolate schedules: `claude-cli` / `codex-cli`. Ignored by `append` mode |
| `model` | isolate yes | `""` | Model for isolate schedules. Ignored by `append` mode |
| `timeout_seconds` | no | `1800` | Isolate timeout in seconds. On timeout, BoxAgent stops the child process, records a failed run log, and allows later cron ticks to continue |
| `enabled_on_nodes` | no | `""` | Only run on matching nodes (matches `node_id` from `local.yaml`); accepts string or list |
| `enabled` | no | `true` | Enable/disable without deleting |

Reserved key: top-level `node_overrides` is metadata, not a task id.

### Execution Modes

- **isolate**: Runs the task in a fresh backend invocation. `ai_backend` and `model` are required. `timeout_seconds` defaults to `1800`; on timeout, BoxAgent stops the isolate child process, writes a failed run log entry, and clears the scheduler's in-memory executing state so future runs are not blocked forever.
- **append**: Queues the prompt into a bot's existing primary session. Shares that bot's current conversation context and always uses that bot's configured backend/model/session. `ai_backend` and `model` are optional fields but ignored in append mode.

### CLI Management

```bash
# Add a schedule
boxagent schedule add --id daily-report --cron "0 9 * * *" --prompt "Check disk usage" --mode isolate --ai-backend claude-cli --model sonnet --timeout-seconds 900

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

## Run

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

## Configuration Reference

### Environment Overrides

```bash
BOXAGENT_MY_BOT_workspace=/data/projects
BOXAGENT_GLOBAL_LOG_LEVEL=debug
BOXAGENT_GLOBAL_API_PORT=8888
```

Per-bot override keys follow `BOXAGENT_<UPPER_BOT_NAME>_<KEY>` (hyphens → underscores). Today only `workspace` is wired up in code.

### CLI Options

```bash
uv run boxagent --config /path/to/config/dir
```

## Development

```bash
# Unit tests (default — runs on every change)
uv run pytest

# With verbose output
uv run pytest -v

# Integration tests (real Claude CLI subprocess)
uv run pytest -m integration

# E2E tests (real Telegram + gateway flow — requires bot token + chat ID)
BOXAGENT_TEST_BOT_TOKEN="..." BOXAGENT_TEST_CHAT_ID="..." uv run pytest -m integration
```

### Project Structure

```
src/boxagent/
├── main.py                  # Entry point, CLI args, signal handling
├── gateway.py               # Gateway composition root + InternalApiServer
├── config.py                # YAML config loading + validation
├── doctor.py                # `doctor --fix` env check + dep installer
├── watchdog.py              # Per-bot process liveness monitor
├── agent_env.py             # AgentEnv (per-turn agent context)
├── utils.py                 # Shared helpers
│
├── agent/                   # Backend adapters + per-bot orchestration
│   ├── protocol.py            # AgentBackend Protocol + BACKEND_KINDS
│   ├── backend_factory.py     # create_backend() dispatches by ai_backend
│   ├── agent_manager.py       # AgentManager: per-bot startup/restart
│   ├── workspace.py           # ensure_git_repo / sync_skills
│   ├── base_cli.py            # Shared subprocess-per-turn base
│   ├── claude_process.py      # Claude CLI backend
│   ├── codex_process.py       # Codex CLI backend
│   ├── sdk_claude_process.py  # claude_agent_sdk (in-process)
│   ├── sdk_copilot_process.py # GitHub Copilot SDK (in-process)
│   ├── callback.py            # AgentCallback Protocol
│   └── mcp_endpoints.py       # pick_mcp_endpoints() — shared MCP wiring
│
├── transports/              # External interaction (channels)
│   ├── base.py                # Channel Protocol + IncomingMessage / Attachment
│   ├── telegram/              # Telegram bot (aiogram 3) + markdown / splitter
│   ├── web/                   # Web UI: SSE channel + HTTP server + static/
│   └── mcp/                   # MCP HTTP server (create_mcp_app + McpHttpServer)
│
├── router/                  # Per-bot session control (auth, /-cmds, dispatch)
│   ├── core.py                # Router class
│   ├── callback.py            # ChannelCallback adapting agent → channel
│   ├── context.py             # First-message session context builder
│   ├── env_builder.py         # IncomingMessage → AgentEnv
│   └── commands/              # /-command handlers (auto-discovered)
│       ├── registry.py          # @command decorator + COMMAND_REGISTRY
│       ├── info.py              # /status /help /version /verbose
│       ├── session.py           # /new /cancel /resume /compact /model /cd /backend
│       ├── tools.py             # /exec /schedule
│       └── workspace.py         # /sessions /trust_workspace /sync_skills
│
├── sessions/                # chat ↔ session_id binding + browse
│   ├── storage.py             # session_history.yaml + transcripts
│   ├── base_pool.py           # BaseSessionPool (chat ↔ backend mapping)
│   ├── pool.py                # SessionPool (pre-warmed, shared)
│   ├── raw_pool.py            # RawSessionPool (per-chat lazy)
│   └── browser/               # /sessions + /resume helpers (merges history + Storage)
│
├── history/                 # Read-only adapters over backends' native session storage
│   ├── protocol.py            # AgentHistory Protocol
│   ├── claude.py              # Reads ~/.claude/projects/
│   ├── codex.py               # Reads ~/.codex/sessions/
│   ├── copilot.py             # Copilot SDK sessions
│   └── factory.py             # get_history(backend)
│
├── tools/                   # Unified MCP tool registry
│   ├── registry.py            # @boxagent_tool + tools_for() / env_capabilities()
│   ├── builtin/               # Tool definitions (admin/peer/schedule/sessions/telegram_media)
│   └── adapters/              # Backend-specific MCP wrappers
│       ├── mcp_http.py          # registry → FastMCP HTTP (claude-cli / codex-cli)
│       ├── claude_sdk.py        # registry → SdkMcpServer (agent-sdk-claude)
│       └── copilot_sdk.py       # registry → native Tool list (agent-sdk-copilot)
│
├── scheduler/               # Cron-based task scheduler
│   ├── engine.py              # Scheduler loop + catch-up
│   ├── cli.py                 # `boxagent schedule` subcommands
│   └── http_routes.py         # POST /api/schedule/run handler
│
├── cluster/                 # Multi-machine networking (hub-and-spoke)
│   ├── tunnel.py              # Host-side devtunnel lifecycle
│   ├── devtunnel.py           # Devtunnel CLI helpers
│   ├── host_election.py       # Host vs guest election + failover
│   ├── registry.py            # Host: GuestRegistry + GuestSession (wire protocol)
│   ├── guest_client.py        # Guest: dial + RPC forwarding
│   ├── peer_service.py        # send_to_peer cross-admin messaging
│   ├── rpc.py                 # Host↔guest HTTP/SSE proxy
│   ├── http_routes.py         # Cluster HTTP routes (peer/send, guest/ws)
│   └── topology_service.py    # Peer descriptors + machine snapshots
│
├── workgroup/               # Multi-agent collaboration (admin + specialists)
│   ├── manager.py             # WorkgroupManager: admin + specialist orchestration
│   ├── http_routes.py         # Workgroup HTTP routes (specialist CRUD, send)
│   ├── channel_adapter.py     # Workgroup transport adapter (Web + Null)
│   ├── heartbeat.py           # HeartbeatManager (admin self-driver)
│   ├── task_queue.py          # SpecialistTaskQueue
│   ├── persistence.py         # workgroup_specialists.yaml
│   ├── specialist_skills.py   # Template-driven skill linking
│   ├── template_loader.py     # Template discovery + loading
│   ├── workspace_templates.py # admin/specialist workspace seeding
│   └── templates/             # Built-in admin / specialist templates
│
└── testing/
    └── mocks.py             # MockBackend / MockChannel — canonical test doubles

ios/BoxAgent/                # Native iOS client (SwiftUI, separate target)
```

Tests live under `tests/unit/` (run by default) and `tests/integration/` (opt-in with `-m integration`). For deeper module-level docs see `docs/codebase-guide.md`.
