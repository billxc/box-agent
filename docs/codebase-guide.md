# BoxAgent 代码库导读

这份文档面向后续维护者，目标不是教你"怎么用"，而是帮你快速建立对当前实现的真实心智模型。

文中提到的 BoxAgent，简称 BA。

结论先说在前面：

- 这份文档以当前代码为准。
- 如果这份文档和其他文档冲突，先信源码，再回头修文档。

## 当前仓库到底实现了什么

当前 BoxAgent 的真实范围比最早设计稿收敛得多，核心是：

- 单进程管理多个 Telegram bot。
- 每个 bot 绑定一个主 AI backend，当前支持 `claude-cli`、`codex-cli` 和 `codex-acp`。
- 普通消息走 `Telegram -> Router -> backend -> Telegram`。
- 支持定时任务，模式分为 `append` 和 `isolate`。
- 有 watchdog、会话存储、内部 HTTP API、Telegram 媒体 MCP 工具。

当前没有落地的设计稿内容主要包括：

- Web UI channel
- Git 同步管理
- LiteLLM / 自定义 Python backend
- 知识库与偏好系统
- 真正意义上的多 worker backend pool

## 建议阅读顺序

如果你要改逻辑，不要从历史设计稿开始。推荐顺序：

1. [`README.md`](../README.md)：部署方式、配置入口、命令面。
2. [`src/boxagent/main.py`](../src/boxagent/main.py)：程序入口。
4. [`src/boxagent/gateway.py`](../src/boxagent/gateway.py)：运行时总装配。
5. [`src/boxagent/router.py`](../src/boxagent/router.py)：消息分发、命令处理、typing 生命周期。
6. 根据 backend 选读 [`src/boxagent/agent/claude_process.py`](../src/boxagent/agent/claude_process.py)、[`src/boxagent/agent/codex_process.py`](../src/boxagent/agent/codex_process.py) 或 [`src/boxagent/agent/acp_process.py`](../src/boxagent/agent/acp_process.py)。共享基类在 [`src/boxagent/agent/base_cli.py`](../src/boxagent/agent/base_cli.py)。
7. [`src/boxagent/channels/telegram.py`](../src/boxagent/channels/telegram.py)：Telegram 输入输出与流式编辑。
8. [`src/boxagent/scheduler.py`](../src/boxagent/scheduler.py) 和 [`src/boxagent/schedule_cli.py`](../src/boxagent/schedule_cli.py)：定时任务与手动触发链路。
9. 对应单测：先看 `tests/unit/`，再看 `tests/integration/`。

## 顶层结构

| 路径 | 作用 |
|------|------|
| `src/boxagent/` | 运行时代码 |
| `src/boxagent/paths.py` | 路径解析集中入口（`resolve_boxagent_dir`、`default_config_dir`、`default_local_dir`、`default_workspace_dir`） |
| `src/boxagent/agent/` | 三种 AI backend 适配层 |
| `src/boxagent/channels/` | Channel 抽象与 Telegram/Discord 实现（含 `md_format.py` 格式转换器） |
| `tests/unit/` | 单测，覆盖绝大多数行为语义 |
| `tests/integration/` | 真实 CLI / E2E 冒烟 |
| `docs/` | 设计文档、使用文档、问题分析 |
| `README.md` | 英文入口说明 |
| `pyproject.toml` | 依赖、入口命令、pytest 默认行为 |

## 运行时结构图

```text
Telegram
  |
  v
TelegramChannel
  |
  v
Router
  |-- commands (/status /new /cancel /compact /model /exec ...)
  |
  |-- ClaudeProcess     -> claude --output-format stream-json ...
  |
  |-- CodexProcess      -> codex exec --json ...
  |
  `-- ACPProcess        -> codex-acp

Gateway
  |-- Storage
  |-- Watchdog (per bot)
  |-- Scheduler
  `-- HTTP API (/api/schedule/run)
```

真正负责装配这些对象的是 `Gateway`，而不是 `main.py`。`main.py` 只做 CLI 参数解析、配置加载、日志初始化、信号处理和 `Gateway.start()/stop()`。

## 一条 Telegram 消息是怎么跑完的

### 1. 启动阶段

`main.py` 做三件事：

- 解析 `--config` 和 `schedule` 子命令。
- 读 `config.yaml`，失败时直接退出。
- 运行 `Gateway`，并在 `SIGINT` / `SIGTERM` 时优雅停机。

`Gateway.start()` 会继续做这些事：

- 创建 `Storage`。
- 为每个 bot 创建 backend、channel、router。
- 如果 bot 配了 `extra_skill_dirs`，把其中各子目录 symlink 到 backend-specific skills 目录：`codex-acp` / `codex-cli` -> `{workspace}/.agents/skills/`，`claude-cli` -> `{workspace}/.claude/skills/`。
- 启动 scheduler。
- 启动内部 HTTP API：
  - 默认监听 Unix socket：`~/.boxagent/local/api.sock`
  - 如果设置了 `BOX_AGENT_DIR` 或 `--box-agent-dir` / `--ba-dir`，socket 路径跟着对应实例的 `local/` 目录走
  - 当 `global.api_port` 非零时额外监听 `127.0.0.1:<port>`

### 2. Telegram 收消息

`TelegramChannel._handle_incoming()` 做的事情很直接：

- 下载照片和文档到临时目录。
- 把它们封装成 `Attachment`。
- 组装成 `IncomingMessage`。
- 回调到 `Router.handle_message()`。

这里有两个现实约束：

- 只有照片和 document 会被下载；其他 Telegram 媒体类型目前没有接进 prompt。
- 附件进入 prompt 的方式不是上传给 backend，而是写成文件路径提示，例如 `[Attached file: /tmp/... ]`。

### 3. Router 分流

`Router.handle_message()` 是真正的请求入口，先做：

- `allowed_users` 鉴权。
- 空消息过滤。
- slash command 识别。

系统命令不会进入 backend 队列，而是立即执行。这一点很重要，因为很多测试都在保护"命令不应该启动 typing loop，也不应该被排队"。

### 4. 普通消息 dispatch

`Router._dispatch()` 当前做的事按顺序是：

1. 如果有 `_compact_summary`，先把摘要前缀拼到 prompt 最前面。
2. 解析单条消息的 `@model` 前缀。
3. 把文本和附件路径拼成一个最终 prompt。
4. 创建 `ChannelCallback`。
5. 先启动 typing loop，再调用 backend `send()`。
6. turn 结束后关闭 stream。
7. 如果拿到了 `session_id`，通过 `Storage` 持久化到 `sessions.yaml`。

注意三个关键事实：

- `_compact_summary` 和 `_resume_context` 都是"发送前消费"的，而不是"成功后消费"的；如果 `backend.send()` 抛异常，这段上下文会永久丢失。
- Router 会把拿到的 `session_id` 统统写进 `sessions.yaml`，即使这个 session 对某些 backend 并不能跨重启恢复。
- `_dispatch()` 中还会消费 `_resume_context`（来自 `/resume` 命令），消费时机和 `_compact_summary` 相同。

### 5. Channel callback 如何映射输出

`ChannelCallback` 是 Router 和 TelegramChannel 之间的桥。

- `on_stream()`：
  - 停 typing
  - 首次输出时创建占位消息
  - 后续通过 `stream_update()` 持续编辑同一条消息
- `on_tool_call()`：
  - 调用 channel 的 `format_tool_call()`
  - 如果已经在流式消息里，就把工具调用插进同一条 stream
  - 然后重新启动 typing，因为工具执行可能很慢
- `on_error()`：
  - 停 typing
  - 结束 stream
  - 再单发一条错误消息
- `on_file()` / `on_image()`：
  - 定义在 `AgentCallback` Protocol 中
  - 映射到 `Channel.send_file()` / `Channel.send_image()`
  - 当前主要由 backend 在处理特定输出时触发

`ChannelCallback` 内部有一个 close guard (`_closed`) 防止重复关闭 stream。它承担了 typing lifecycle、stream handle 管理、工具调用格式化等多重职责。

typing loop 每 4 秒发送一次 `ChatAction.TYPING`。这套生命周期在 `tests/unit/test_typing_indicator.py` 里测得很细。

## 三种 backend 的真实语义

| 维度 | `claude-cli` | `codex-cli` | `codex-acp` |
|------|--------------|-------------|-------------|
| 主实现 | `ClaudeProcess` | `CodexProcess` | `ACPProcess` |
| 运行方式 | 每轮 spawn 一个 `claude` 子进程 | 每轮 spawn 一个 `codex exec` 子进程 | 维持一条 `codex-acp` ACP 连接 |
| 连续对话 | 靠 `--resume <session_id>` | 靠 `codex exec resume <thread_id>` | 同一进程内用同一 ACP session 续接；跨重启时优先用 `load_session(session_id, cwd)` 恢复 |
| 跨 gateway 重启恢复 | 可以 | 可以（`sessions.yaml` 保存 `thread_id`，重启后 resume） | 可以（当前通过 `load_session(session_id, cwd)` 恢复；失败时 fallback 新 session） |
| `/cancel` 后会话语义 | 终止当前子进程，保留 `session_id` 字段，后续仍可继续 `--resume` | 终止当前子进程，保留 `session_id`，后续仍可 resume | 向当前 ACP session 发 `session/cancel`；若失败则断开 transport，下一轮干净开始 |
| `/new` / `/compact` | 走 `reset_session()`，清掉 `session_id` | 走 `reset_session()`，清掉 `session_id` | 走 `reset_session()`，断开 ACP 连接并丢弃 `_acp_session_id` |
| 模型切换 | 每轮可透传 `--model` | 每轮可透传 `--model` | 只在启动新的 ACP session 时生效 |
| `agent` 字段 | 透传 `--agent` | 当前忽略 | 当前忽略 |
| 技能目录 | `.claude/skills/` | `.agents/skills/` | `.agents/skills/` |
| 输出格式 | NDJSON (stream-json) | JSONL (`--json`) | ACP session_update events |
| 媒体 MCP | 通过 `--mcp-config` 注入 Telegram 媒体 MCP server | 未实现（Codex exec 非交互模式限制） | 当前仍沿用 ACP tool lifecycle 展示 |

### `ClaudeProcess` 需要记住的点

- 继承自 `BaseCLIProcess`，共享队列/状态机/cancel/stop 逻辑。
- 只实现 `_build_args()` 和 `_parse_event()`。
- 每轮执行命令形如：
  - `claude --output-format stream-json --verbose --dangerously-skip-permissions -p ...`
- 同时兼容两种 Claude 输出格式：
  - `assistant.message.content` 批量块
  - `content_block_*` 增量流
- 工具调用展示依赖 callback，而不是 CLI 自己格式化。

### `CodexProcess` 需要记住的点

- 同样继承自 `BaseCLIProcess`。
- 每轮执行命令形如：
  - 新 session：`codex exec --json --color never --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C <workspace> <prompt>`
  - 续接：`codex exec resume --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check <thread_id> <prompt>`
- 注意 resume 子命令的参数集比 exec 小很多（没有 `--color`、`--sandbox`、`-C`）。
- 输出是 JSONL，关键事件类型：`thread.started`、`item.started`（tool 开始）、`item.completed`（文本或 tool 完成）、`turn.completed`。
- `session_id` 来自 `thread.started` 事件中的 `thread_id`。

## `/sessions` 命令 — 统一会话浏览

`/sessions` 合并三个数据源，提供统一的会话浏览和搜索：

### 数据源

1. **Claude CLI sessions** — `~/.claude/projects/*/sessions-index.json` + 未索引的 `*.jsonl`
2. **BoxAgent session history** — `~/.boxagent/local/session_history.yaml`（含 backend、model、bot 等元数据）
3. **Codex rollout sessions** — `~/.codex/sessions/` 下的 rollout JSONL 文件

三者按 `sessionId` 去重合并，BoxAgent history 的元数据（backend/model/bot/preview）会覆盖到 Claude 条目上。

### Token 解析

`/sessions` 后的参数按优先级分类：

| 优先级 | 模式 | 示例 | 含义 |
|--------|------|------|------|
| 1 | `pN` | `p2` | 翻页 |
| 2 | `Nd` | `3d`, `30d` | 最近 N 天 |
| 3 | `backend:X` | `backend:codex-cli` | 按 backend 过滤 |
| 4 | `bot:X` | `bot:claw-mac` | 按 bot 过滤 |
| 5 | 4+ 位 hex 且匹配 session ID 前缀 | `aa3f` | 直接定位 session |
| 6 | 其余 | `chromium`, `discord fix` | 搜索关键词（多词 AND，多字段 OR） |

### 三端入口

- **Chat**（Telegram/Discord）：`router_commands.cmd_sessions()` → `format_sessions_list(storage=self.storage)`
- **MCP**：`mcp_server.sessions_list()` → `format_sessions_list(storage=Storage(LOCAL_DIR))`
- **CLI**：`sessions_cli.sessions_list(args)` → `_load_all_unified_sessions(storage=...)`

### 与 `/resume` 的关系

`/sessions` 输出中每条会话附带 `/resume <session_id>` 命令，用户复制粘贴即可恢复。`/resume` 保持独立，负责实际的会话恢复逻辑。

## `/resume` 命令与 Codex 软恢复

`/resume` 命令有两条完全不同的分支：

### 会话恢复

`claude-cli` 和 `codex-cli` 都继承自 `BaseCLIProcess`，共享队列/状态机/cancel/stop 逻辑，只实现 `_build_args()` 和 `_parse_event()`。

Codex ACP 这边要区分两层语义：
- **native resume**：`codex-acp` 现在会在 gateway / watchdog 重建后，优先使用保存的 `session_id + cwd` 调 `load_session()` 恢复原生会话；如果 `load_session()` 失败，再 fallback 到新 session。
- **soft resume**：Router / Storage 里仍保留基于本地 rollout 日志恢复上下文的软恢复路径。它不是原 session 挂回，而是“新 session + 注入恢复上下文”。

## 定时任务链路

### 文件与入口

- 调度定义文件：`~/.boxagent/schedules.yaml`
- 运行时实现：[`src/boxagent/scheduler.py`](../src/boxagent/scheduler.py)
- CLI 管理入口：[`src/boxagent/schedule_cli.py`](../src/boxagent/schedule_cli.py)
- 手动触发 API：`POST /api/schedule/run`

`boxagent schedule run --id ...` 不会自己执行业务逻辑，而是去调用 gateway 的内部 API：

- 优先走 Unix socket `~/.boxagent/local/api.sock`
- 如果 `api_port` 已配置，再回退尝试本地 TCP

### scheduler 每分钟做什么

`Scheduler.run_forever()` 的语义很简单：

- 睡到下一个整分钟。
- 重新从磁盘加载 `schedules.yaml`，并按当前 `node_id` 应用 `node_overrides`。
- 根据 `_last_check` 计算这次要检查的分钟列表。
- 对匹配 cron 的任务 `create_task(self._fire(task))`。
- 用 `_executing` 防止同一个任务并发重入；如果任务还没结束，新一轮 cron 会记 warning 并跳过本次触发。

这里的设计重点有两个：

- 调度文件是热加载的，不需要重启 gateway。
- catch-up 只依赖内存里的 `_last_check`，没有持久化的 scheduler state。

### `append` 和 `isolate` 的差别

`append`：

- 直接把 prompt 送进 bot 的主 backend。
- 共享该 bot 的当前会话上下文和 backend 语义。
- 会先发一条"Append task started"消息，再发结果。

`isolate`：

- 起一个全新的独立 backend 调用，不复用 bot 当前会话。
- `ai_backend` 和 `model` 由任务配置显式指定。
- 如果 `task.bot` 指向某个 bot，就只借用它的通知通道解析，不继承该 bot 的会话或 backend 状态。
- isolate 调用默认有 `1800s` timeout，可由 `timeout_seconds` 覆盖；超时会 stop 子进程、写失败 run log，并释放 `_executing`。

### isolate 模式当前的几个现实限制

这些点对维护者很关键，因为名字容易让人误以为"隔离任务继承了 bot 的全部配置"，但当前并不是：

- isolate 使用 scheduler 自己的 workspace，不继承目标 bot 的 workspace / 会话状态。
- isolate 模式不会注入 Telegram 媒体 MCP 工具。
- `append` 模式才会真正跑在 bot 的长生命周期 backend 上。

## 配置与持久化

### 配置文件

主配置文件是 `~/.boxagent/config.yaml`，解析入口在 [`src/boxagent/config.py`](../src/boxagent/config.py)。

可选的 `~/.boxagent/telegram_bots.yaml` 提供 `bot_id` → token 映射，允许 `config.yaml` 中通过 `bot_id` 引用而非直接写 token。解析优先级：`token` > `bot_id` > 报错。

当前代码里真正接线的字段包括：

- 全局：
  - `log_level`
  - `api_port`
  - `node_id`（从 `{local_dir}/local.yaml` 加载）
  - `node_overrides.<node_id>.global.*`（按节点覆盖）
- bot：
  - `ai_backend`
  - `workspace`
  - `telegram token`（或 `bot_id`，从 `telegram_bots.yaml` 查找 token）
  - `allowed_users`
  - `model`
  - `agent`
  - `extra_skill_dirs`
  - `display.tool_calls`
  - `enabled_on_nodes`（可选，节点过滤）
  - `node_overrides.<node_id>.bots.<bot>.*`（按节点覆盖）

### 环境变量覆盖的真实情况

代码里虽然保留了通用命名规则，但当前真正生效的 override 只有：

- `BOXAGENT_<BOT>_workspace`
- `BOXAGENT_GLOBAL_LOG_LEVEL`
- `BOXAGENT_GLOBAL_API_PORT`

不要把 README 里的"命名约定"误读成"所有 bot 字段都支持 env override"。

### 本地状态目录

`~/.boxagent/local/` 里当前主要涉及：

- `sessions.yaml`
- `session_history.yaml`
- `transcripts/` - 对话日志（每个 session 一个 JSONL 文件）
- `api.sock`

其中：

- `Storage` 直接管理的是 `sessions.yaml` 和 `session_history.yaml`
- `transcripts/` 由 `Router` 在每轮对话结束后追加写入
- `api.sock` 由 `Gateway` 的内部 HTTP API 创建和清理

真实语义如下：

- `sessions.yaml` 会保存每个 bot 的 `session_id`。
- gateway 启动时只会把这个 session 重新喂给支持持久恢复的 backend。
- 对 `codex-acp`，gateway 停机时会写下 `session_id`；重启时会把这个 `session_id` 连同 workspace 一起传给 `ACPProcess`，优先调用 `load_session()` 恢复旧 ACP session。

### 对话日志 (Transcript)

每轮对话结束后，`Router._dispatch` 调用 `_log_turn()` 将 user 和 assistant 文本追加写入 `{local_dir}/transcripts/{session_id}.jsonl`。每行是一个 JSON 对象，包含 `ts`、`bot`、`chat_id`、`event`（user/assistant）、`text`。

当前限制：只在 send 成功后记录。send 失败的 turn 不会被记录（见 BUG006）。

## 看门狗与重启语义

`Watchdog.run_forever()` 每 30 秒检查一次 backend `state`。

当它看到 `state == "dead"` 时会：

- 向默认 chat 发一条"即将重启"的消息。
- 等待 `restart_delay`，默认 5 秒。
- 调 `Gateway._restart_bot()`。

`Gateway._restart_bot()` 当前会更新：

- `self._cli_processes[name]`
- 对应 `Router.cli_process`
- 对应 `Scheduler.bot_refs[name].cli_process`

但它不会更新已经创建好的 `Watchdog.cli_process` 引用。这意味着如果你后面要改 watchdog / restart 逻辑，这里必须重点复核；当前实现更接近"路由层和 scheduler 换了新 backend"，而不是"整条 watchdog 监控链都换成新 backend"。

## Telegram 输出层细节

[`src/boxagent/channels/telegram.py`](../src/boxagent/channels/telegram.py) 值得单独看，因为很多用户感知都在这里。

它现在做了这些事情：

- 使用 MarkdownV2 格式发送消息，由 [`src/boxagent/channels/md_format.py`](../src/boxagent/channels/md_format.py) 的单 pass tokenizer 转换（正则一趟扫描 code fence、table、inline code、bold、italic、strikethrough、link，三套转义上下文）。
- 长消息按 4096 字符限制拆分。
- 尽量在段落和换行处拆，避免拆进代码块。
- 流式输出通过编辑同一条消息完成。
- 节流参数是：
  - 最多每 300ms 刷一次
  - 或者缓冲新增字符达到 200 时立刻刷
- Markdown 发送失败会降级成 plain text 再发一次。

工具调用展示完全由 `tool_calls_display` 控制：

- `silent`
- `summary`
- `detailed`

这个行为在 `tests/unit/test_display.py` 和 `tests/unit/test_telegram_channel.py` 里都有覆盖。

## 哪些文件最值得先看

| 文件 | 为什么重要 |
|------|------------|
| `src/boxagent/gateway.py` | 所有组件从这里装起来，启动、重启、停机都在这里 |
| `src/boxagent/router.py` | 命令、dispatch、typing、session 持久化都在这里汇合 |
| `src/boxagent/agent/acp_process.py` | 当前 ACP backend 的主过程层，后续能力扩展集中在这里 |
| `src/boxagent/agent/claude_process.py` | Claude turn 生命周期和 `--resume` 的真实入口 |
| `src/boxagent/scheduler.py` | append / isolate 的差异、catch-up、热加载 |
| `src/boxagent/channels/telegram.py` | 用户实际看到的输出行为 |

## 改代码时最容易踩坑的地方

- 命令路径和普通消息路径是分开的。不要把 slash command 改成也走 backend 队列，很多 typing / 并发语义会被打坏。
- `Router` 会在每个 turn 后持久化 `session_id`，但"持久化了"不等于"下次一定能恢复"。
- `codex-acp` 的 `session_id` 现在已经参与跨重启恢复，但前提是与正确的 workspace 一起传给 `load_session(session_id, cwd)`；恢复失败时仍会 fallback 到新 session。
- `_compact_summary` 当前是一次性前缀，而且会在真正发送前被消费掉。
- isolate scheduler 名义上是"独立任务"，但当前没有继承 bot 的完整运行上下文。
- watchdog 重启后谁持有新 backend 引用，当前不是全链路一致的。

## 已知缺陷与待修复项

以下是 2026-03-23 审计中确认的问题，按严重程度排列：

### Bug 级别

1. **Watchdog 重启后引用失效**（最严重）
   - `Gateway._restart_bot()` 更新了 `_cli_processes`、`Router.cli_process`、`Scheduler.bot_refs[].cli_process`，但没有更新 `Watchdog.cli_process`。
   - 后果：重启后 Watchdog 持有旧的 dead backend 引用，会持续检测到 dead 状态并无限循环触发重启。
   - 测试盲点：`test_gateway.py` 的 `test_restart_bot_updates_scheduler_ref` 只验证了 scheduler 引用更新，没有验证 watchdog 引用。

2. **`_compact_summary` / `_resume_context` 失败时丢失**
   - 两者在 `_dispatch()` 中 `backend.send()` 调用前被消费（赋值给局部变量并将字段置 None）。
   - 后果：如果 send 抛异常，上下文永久丢失，无法重试。
   - 建议：改为在 send 成功后才清除字段，或在 except 块中恢复。

### 已清理的死代码

以下字段已在 2026-03-22 删除：`max_workers`、`display.streaming`、PID 跟踪。

### 未完成的功能

- **路径布局迁移**：`paths.py` 中有 TODO，计划将 `{config_dir}-local` sibling 布局迁移到 `{config_dir}/local`。

### 健壮性问题

- **优雅停机后孤儿子进程**：`main.py` 中优雅停机使用 10 秒 `asyncio.wait_for()` 超时，但超时后没有显式 kill 所有已知子进程。
- **Scheduler catch-up 静默跳过**：`max_catchup=5` 意味着 gateway 停机超过 5 分钟时，中间的定时任务被静默跳过，没有日志记录被跳过的任务。
- **schedule_cli 的异常处理不完整**：`schedule_run()` 只捕获了 `ConnectError`，没有处理 `TimeoutError` 等其他 httpx 异常。

### 测试覆盖盲点

- Watchdog 重启后引用失效问题没有测试覆盖。
- `_dispatch()` 中上下文失败丢失问题没有测试覆盖。
- Scheduler catch-up 跳过超过 5 分钟的任务时，没有测试验证告警行为。
- `on_file` / `on_image` 回调在 `ChannelCallback` 中的调用路径没有单测覆盖。

## 测试地图

这套仓库的测试密度很高，很多语义直接写在测试里。修改行为前最好先读对应测试。

| 测试文件 | 重点覆盖 |
|----------|----------|
| `tests/unit/test_claude_process.py` | Claude stream-json 解析、cancel、队列、MCP config |
| `tests/unit/test_codex_process.py` | Codex CLI JSONL 解析、resume、cancel |
| `tests/unit/test_acp_process.py` | ACP 事件映射、tool lifecycle、cancel 清理 |
| `tests/unit/test_base_cli.py` | BaseCLIProcess 命令 shim 解析 |
| `tests/unit/test_config.py` | 配置解析和 env override |
| `tests/unit/test_context.py` | Session 上下文构建与字段注入 |
| `tests/unit/test_router.py` | auth、命令识别、dispatch |
| `tests/unit/test_router_cancel_integration.py` | Router 级 /cancel 与 backend 状态管理 |
| `tests/unit/test_router_late_stream_race.py` | 迟到的 stream chunk 竞态回归测试 |
| `tests/unit/test_commands.py` | `/status` `/new` `/cancel` `/compact` `/model` `/exec` |
| `tests/unit/test_gateway.py` | 组件装配、stop、restart、HTTP API |
| `tests/unit/test_storage.py` | session 辅助逻辑 |
| `tests/unit/test_watchdog.py` | dead process 检测与通知 |
| `tests/unit/test_splitter.py` | 长消息拆分与 code fence 保护 |
| `tests/unit/test_md_format.py` | Markdown 格式转换器（Telegram MarkdownV2 + Discord） |
| `tests/unit/test_telegram_channel.py` | Telegram 发送、流式编辑、tool display |
| `tests/unit/test_typing_indicator.py` | typing loop 的完整生命周期 |
| `tests/unit/test_display.py` | `/verbose` 与 `format_tool_call()` |
| `tests/unit/test_scheduler.py` | cron 装载、catch-up、append / isolate |
| `tests/unit/test_schedule_cli.py` | `boxagent schedule ...` 子命令 |
| `tests/unit/test_mcp_server.py` | Telegram 媒体 MCP 工具 |
| `tests/unit/test_harness_judge.py` | 规则化 harness 结果判定 |
| `tests/unit/test_main.py` | CLI 入口、--ba-dir 参数解析 |
| `tests/integration/test_cli_real.py` | 真实 Claude CLI turn |
| `tests/integration/test_e2e.py` | 端到端主链路冒烟 |

`pyproject.toml` 默认会跳过 integration tests，所以平时 `uv run pytest` 主要跑的是单测。

## 模块依赖图

```text
main.py
  ├── config.py        (load_config)
  ├── paths.py         (resolve_boxagent_dir, default_config_dir, default_local_dir)
  ├── schedule_cli.py  (schedule 子命令分发)
  └── gateway.py       (Gateway.start/stop)

gateway.py
  ├── config.py        (AppConfig, BotConfig)
  ├── paths.py         (default_config_dir, default_local_dir)
  ├── storage.py       (Storage)
  ├── router.py        (Router)
  ├── watchdog.py      (Watchdog)
  ├── scheduler.py     (Scheduler, BotRef, load_schedules)
  ├── agent/claude_process.py  (ClaudeProcess)
  ├── agent/codex_process.py   (CodexProcess)
  ├── agent/acp_process.py     (ACPProcess)
  └── channels/telegram.py    (TelegramChannel)

router.py
  ├── router_callback.py       (ChannelCallback, TextCollector, log_turn)
  ├── router_commands.py       (系统命令处理器)
  ├── context.py               (UserContext)
  ├── agent/callback.py       (AgentCallback Protocol)
  ├── agent/claude_process.py  (backend.send/cancel/reset_session)
  ├── channels/base.py        (IncomingMessage, StreamHandle)
  ├── channels/telegram.py    (Channel Protocol 实现)
  └── storage.py              (build_codex_resume_context, save_session)

scheduler.py
  ├── agent/claude_process.py  (backend.send, _spawn_isolate_*)
  ├── channels/base.py        (Channel)
  └── agent/callback.py       (AgentCallback)

schedule_cli.py
  ├── paths.py  (resolve_boxagent_dir, default_config_dir, default_local_dir)
  └── httpx     (HTTP API 调用)

agent/base_cli.py
  ├── agent/callback.py  (AgentCallback)
  └── mcp_server.py      (通过 --mcp-config 注入)

channels/telegram.py
  ├── channels/base.py      (Attachment, IncomingMessage, StreamHandle, Channel)
  ├── channels/md_format.py  (md_to_telegram, md_to_discord — format conversion)
  ├── channels/splitter.py  (split_message)
  └── aiogram 3
```

调用链概要：

```text
[Telegram 用户消息]
  -> TelegramChannel._handle_incoming()
  -> Router.handle_message()
    -> [鉴权] -> [命令分流 或 _dispatch]
    -> _dispatch()
      -> ClaudeProcess.send() 或 ACPProcess.send()
        -> ChannelCallback.on_stream/on_tool_call/on_error
          -> TelegramChannel.stream_start/stream_update/stream_end/send_text
      -> Storage.save_session()

[定时任务]
  -> Scheduler.run_forever()
    -> _fire()
      -> _execute_isolate() 或 _execute_append()
        -> _SchedulerCallback 收集输出
        -> Channel.send_text() 发送结果

[Watchdog]
  -> Watchdog.run_forever()
    -> run_once(): 检测 dead state
      -> Gateway._restart_bot()

[CLI schedule run]
  -> schedule_cli.schedule_run()
    -> httpx POST /api/schedule/run (Unix socket 或 TCP)
      -> Gateway HTTP handler -> Scheduler._execute_once()
```

## 历史文档

早期设计文档已归档清理。如需了解设计演变过程，参考 `docs/decisions.md` 中的决策记录和 git history。

## 一句话总结

当前 BoxAgent 的主线并不复杂：

- `Gateway` 负责装配
- `Router` 负责分流
- `TelegramChannel` 负责用户可见输出
- `ClaudeProcess`、`CodexProcess` 和 `ACPProcess` 负责后端语义
- `Scheduler` 负责定时触发

真正复杂的地方在"状态什么时候还能续接、什么时候只是看起来像续接"，尤其是 `codex-acp`、`/cancel`、`/compact`、watchdog restart 和 isolate scheduler 这几条线。
