# Agent Backends、生命周期与 Session

> 全部依据源码核对（`src/boxagent/agent/` + `sessions/` + `scheduler/`）。

## AgentBackend Protocol

所有 backend 实现 `agent/protocol.py:AgentBackend`（`@runtime_checkable` 结构化 Protocol，
不需要继承）。Router / Watchdog / SessionPool / Scheduler 只透过这个接口跟 backend 打交道。

稳定接口面（`agent/protocol.py:43`）：

- **身份/配置**（可变，`/model` `/cd` `/resume` 会改写）：`bot_name` / `workspace` / `model` / `agent` / `session_id` / `state`（`idle`|`busy`|`dead`）。
- **能力位**：`supports_session_persistence`（能跨重启 resume）、`supports_fork`（能 fork 出兄弟 session 不污染源）、`yolo`（跳过权限确认）。
- **每轮诊断**：`last_turn_failed` / `last_turn_error` —— `send` 不抛异常，把最终成败写这两个字段，Router 读它判断（`router/core.py:237`）。
- **方法**：`start()`（同步非阻塞）/ `stop()` / `send(message, callback, ...)` / `cancel()` / `reset_session()` / `wait_idle()` / `fork_and_send(...)`。

合法 `ai_backend` 字符串的唯一真源：`agent/protocol.py:35 BACKEND_KINDS`。

## 四种 kind，三个实现类

`agent/backend_factory.py:create_backend()` 按 `bot_config.ai_backend` 分发：

| `ai_backend` | 实现类 | 进程模型 | session 持久化 | MCP 挂载 |
|---|---|---|---|---|
| `claude-cli` | **静默重定向到 `AgentSDKClaude`**（`backend_factory.py:54`；旧 config 兼容，`ClaudeProcess` 文件已删除） | in-process | 共享 `~/.claude/` | SDK 直注 |
| `agent-sdk-claude` | `AgentSDKClaude` | in-process（`claude_agent_sdk.query`） | 共享 `~/.claude/` | `SdkMcpServer` |
| `codex-cli` | `CodexProcess` | 每轮 spawn `codex exec` subprocess | `codex exec resume <thread_id>`（Codex 自管） | `-c mcp_servers.X.url=...` |
| `agent-sdk-copilot` | `AgentSDKCopilot` | in-process（共享 `CopilotClient` 子进程） | Copilot SDK `resume_session` | 原生 Tool 对象 |

> **两种并发模型**（重要）：
> - `AgentSDKClaude` / `AgentSDKCopilot` 的 `send()` **直接 inline 跑** —— 串行化靠上层
>   `SessionPool` 一个 chat_id 独占一个 backend 实例。
> - `CodexProcess`（继承 `base_cli.py:BaseCLIProcess`）有**自己的串行消息队列** + `_process_queue`
>   循环，每轮 spawn 一个子进程；`send` 只是把消息 put 进队列再 `await done`。

### AgentSDKClaude（主参考，`sdk_claude_process.py`）

- `send()` 每轮调一次 `query(prompt, options)`，消费 async 消息流翻译成 `AgentCallback` 事件
  （`AssistantMessage`→`on_stream`、`ToolUseBlock`+`ToolResultBlock`→`on_tool_call`、
  `SystemMessage subtype=status/compact_boundary`→`on_compact_event`）。
- session 续期：`options.resume = self.session_id`（`sdk_claude_process.py:270`）。session_id 从
  `AssistantMessage.session_id` 抓回来。
- system prompt：`options.system_prompt = {type:"preset", preset:"claude_code", append: append_system_prompt}`。
- `yolo=True` → `options.permission_mode = "bypassPermissions"`。
- `fork_and_send`：造一个**新的** `AgentSDKClaude` 实例、`_fork_session=True`，不动 self。
- MCP：`build_mcp_servers(ctx, env)`，除非 `env.passthrough`（raw bot 跳过所有注入）。

### CodexProcess（`codex_process.py` + `base_cli.py`）

- 新会话：`codex exec --json --color never --skip-git-repo-check -C <workspace> -`；恢复：`codex exec resume --json ... <session_id> -`。prompt 走 **stdin**（`-` 模式）。
- session_id = `thread.started` 事件里的 `thread_id`。
- system prompt 走 Codex 的 `-c developer_instructions="..."`；MCP 走 `-c mcp_servers.X.url` + `http_headers`。
- `yolo` → `--dangerously-bypass-approvals-and-sandbox`。
- `cancel()` 杀整个进程组（Unix `killpg`，Windows `taskkill /T /F`）—— codex 在 node 下有子进程。
- `supports_fork = False`（base 的 `fork_and_send` 直接 `raise NotImplementedError`）。

### AgentSDKCopilot（`sdk_copilot_process.py`）

- **共享 class 级 `CopilotClient`**（子进程，启动 ~7s），refcount 记数（`_acquire_shared_client` / `_release_shared_client`）。SessionPool size N → N 个 backend 实例，但都 multiplex 到同一个 CLI 子进程。
- 每实例一个 `CopilotSession`，首个 `send` 懒建（或 `resume_session`）。
- **system_message 只能在 create_session 时给**（append 模式），首轮的 append_system_prompt 会粘住整个 session —— 要刷新只能 `reset_session`（`/new` `/compact` `/backend` 都会）。
- 非 yolo 模式 **拒绝所有工具调用**（`_deny_all`，`sdk_copilot_process.py:55`）—— 交互式审批还没接。
- `streaming=True` 才有增量 delta；`fork_and_send` 走 SDK 的 `sessions.fork` RPC。

## AgentManager：per-bot 生命周期 + watchdog

`agent/agent_manager.py:AgentManager` 是 Gateway 的 `self._bots`，拥有所有 per-bot 状态 dict
（`backends` / `pools` / `routers` / `channels` / `web_channels` / `watchdogs`），别的 manager 按
引用读。

- `start_all_for_node(node_id)`：遍历 `config.bots`，`node_matches(enabled_on_nodes, node_id)` 通过的才 `start_bot`；最后 `start_raw_bot`。
- **`start_bot`**（`agent_manager.py:165`）每个 bot 做：
  1. 支持持久化的 backend 从 storage 读回 `session_id`。
  2. `create_backend` + `backend.start()`。
  3. 建 `SessionPool(size=3)`（用 factory 预热 3 个 backend）。
  4. `ensure_git_repo(workspace)`（skill 发现需要 .git）+ `sync_skills`。
  5. 有 `telegram_token` 就建 `TelegramChannel`。
  6. 建 `Router`，把 channel 的 `on_message` 接到 `router.handle_message`。
  7. `web_enabled` 就建 `WebChannel(bot_name, machine_id, message_bus)`。
  8. 建 `Watchdog` 跑 `run_forever`（backend 死了自动 `restart_bot`）。
  9. Telegram 推一条 "🟢 bot online" 启动通知。
- **`start_raw_bot`**：合成的 `"raw"` bot，`passthrough=True` / `yolo=True` / web-only，用
  `RawSessionPool` + `_raw_backend_factory`。raw bot 跳过所有 BoxAgent context/MCP 注入，行为等同直接跑 CLI。
- **`restart_bot`** / **`on_backend_switched`**（`/backend` 命令切 kind）：换 backend 后
  **同步更新 `routers[name].backend`、`scheduler.bot_refs[name].backend`、`watchdogs[name].backend`**
  —— 历史坑"watchdog 持旧 backend 引用"就在这里修的，改这段务必三处一起更。

## Sessions：pool 与持久化

### 两种 pool（`sessions/base_pool.py:BaseSessionPool`）

`chat_id → backend` 映射。基类管 per-chat 状态 + storage round-trip，子类只决定"怎么借/还"：

- **`SessionPool`**（`pool.py`）：固定 size 的预热 backend 队列，一个 pool 一种 backend kind，所有 chat 共享。`start_bot` 用它，size=3。
- **`RawSessionPool`**（`raw_pool.py`）：per-chat 懒生成，每个 chat 可要不同 kind。raw bot 用它。

关键机制（`base_pool.py`）：

- `ChatState`（session_id / model / workspace / backend-kind），首次访问某 chat_id 时从 `Storage.load_session` 懒加载。
- `acquire(chat_id)`：`_borrow` 一个 backend → `_restore_to` 把 ChatState 灌上去 → 记进 `_active`。
- `release(chat_id, backend)`：`_capture_from` 把 turn 后状态抓回 ChatState → `_return`。
- `get_active(chat_id)`：turn 进行中返回那个 backend —— Router 用它判断"这个 chat 忙不忙"（忙就缓冲后续消息，`router/core.py:114`）。

### 持久化（`sessions/storage.py`，写在 `~/.boxagent-local/`）

两个 YAML，别搞混：

- **`sessions.yaml`** —— 权威绑定。key 是 `bot_id` 或 `bot_id:chat_id`，value = `{session_id, previous_session_ids[], workspace, model, backend}`。
  - **链式保存**：同一 chat_id 换新 session_id（如 `/compact`）时，旧 sid 推进 `previous_session_ids`（capped 20），让 transcript reader 跨 compact 拼回历史。这是 BUG88/89 修复的核心，动 compact 流程要跑 `tests/unit/test_session_chain*.py`。
  - `clear_session(preserve_chain=True)` = `/compact` 语义（留链）；`preserve_chain=False` = `/new`（整条删）。
- **`session_history.yaml`** —— `_global` 最近会话列表（capped 50，带 preview / backend / model），给 `/sessions`、`/resume`、Web recents 用。

> BoxAgent 只记 `chat_id → session_id` 绑定，**session 内容由各 backend 自己持久化**
> （Claude/Copilot 在自己目录，Codex 靠 `resume <thread_id>`）。

## Scheduler（`scheduler/engine.py`）

cron 驱动，`Gateway._start_scheduler` 在所有 bot 起来后建，跑 `run_forever` 主循环（按分钟边界醒、
`croniter` 匹配、`node_matches` 过滤 `enabled_on_nodes`）。

`ScheduleTask`（`engine.py:49`）：`id` / `cron` / `prompt` / `mode` / `bot` / `ai_backend` / `model` / `timeout_seconds`（默认 1800）/ `yolo` / `enabled_on_nodes` / `enabled`。

两种模式：

- **`isolate`**（默认）：spawn 一个独立 backend 跑，**必须给 `ai_backend`**。不碰在跑的 bot。
- **`append`**：在某个 bot 的活 backend 上追加一轮，**必须给 `bot`**。

结果：AI 输出里 `<ScheduleResult>...</ScheduleResult>` 会被 `extract_schedule_result` 抽出来推给用户。
触发端点 `POST /api/schedule/run`（`scheduler/http_routes.py`，挂在 InternalApiServer）。CLI 子命令
`boxagent schedule ...`（`scheduler/cli.py`，无 daemon）。

下一步：[Transports](transports.md) 看回复怎么流回 channel。
