# `raw` virtual bot — passthrough跳板

## 目标

提供一个**只在 Web UI 可见**、**不注入任何 BoxAgent 上下文**的虚拟 bot，专门用作
`claude` / `codex` / `copilot` 等后端原生 session 的"跳板"。让用户在 Web UI 里
resume 一个原生 session 时，看到的对话和**直接在终端 `claude --resume <sid>` 里跑
是一致的** —— 没有 `--append-system-prompt`、没有 BoxAgent MCP 工具、没有 bot
身份注入。

## 核心约束

1. **不在配置文件里**：`raw` 是 BoxAgent 启动时合成出来的 BotConfig，用户不需要也
   不能在 `bots.yaml` 里声明它。
2. **Web-only**：不绑任何 Telegram token，外部 IM 物理上无法访问。
3. **多后端共存**：同一个 `raw` bot 下不同 chat 可以分别跑 claude / codex /
   copilot —— backend 在 resume 时由前端选定，写入 `sessions.yaml`，由专用 pool
   按需 lazy spawn。
4. **零注入**：`passthrough=True` 标记会让 router 跳过 system context、resume
   context、compact summary，并让各 backend 的 `_build_args` 跳过
   `--append-system-prompt` 和 `--mcp-config`。
5. **resume 后绑 raw**：通过 `/api/claude/resume`（实际上重命名/泛化为 `/api/resume`）
   把 `chat_id = "claude-{sid}"`（或 `"codex-{sid}"` / `"copilot-{sid}"`）绑到
   `raw` bot 名下，写入 sessions.yaml；以后这个 chat_id 永远走 raw bot。

## 架构变更点

### 1. `BotConfig.passthrough: bool = False`

`src/boxagent/config.py:23` 处的 dataclass 新增字段。仅 `raw` bot 设为 True。

### 2. `AgentEnv.passthrough: bool = False`

`src/boxagent/agent_env.py:131` 同步加字段。Router 构造 env 时从 BotConfig 透传。

### 3. Router 守卫

`src/boxagent/router/core.py` 的 `_dispatch_one`（约 571 行起）：

```python
if not env.passthrough:
    context = self._build_session_context(chat_id, env=env)
    if context: system_parts.append(context)
    resume_ctx = self._resume_contexts.pop(chat_id, "")
    if resume_ctx: system_parts.append(resume_ctx)
    compact_summary = self._compact_summaries.get(chat_id, "")
    if compact_summary: system_parts.append(...)
# passthrough 时 system_parts 全程为空
```

### 4. Backend `_build_args` 守卫

每个 backend 的 `_build_args` 接收 `env`，在 passthrough 时跳过额外注入：

| 文件 | 跳过项 |
|---|---|
| `agent/claude_process.py:71`, `:84` | `--append-system-prompt`、`--mcp-config` |
| `agent/codex_process.py:50` | `--append-system-prompt` 等价 |
| `agent/acp_process.py:387` | session_update 注入的 system 段 |

### 5. 虚拟 raw bot 注册

`src/boxagent/gateway.py` 启动阶段，在 `_load_config` / `start` 加载完真实 bot 后：

```python
self.config.bots["raw"] = BotConfig(
    name="raw",
    ai_backend="claude-cli",  # 占位，per-chat 覆盖
    workspace="",             # per-chat 覆盖（来自 resume）
    passthrough=True,
    web_enabled=True,
)
```

然后 `_start_bot("raw", ...)` 走特殊分支：**不创建普通 SessionPool**，改创建
`RawSessionPool`（见下）；不订 Telegram（自然无 token）。

### 6. `RawSessionPool`

新文件 `src/boxagent/sessions/raw_pool.py`，接口与 `SessionPool` 兼容
（`acquire / release / set_session_id / set_workspace / get_session_id /
all_processes / stop / restart_dead`），但实现差异：

- **不预 spawn**：构造时不开任何 process。
- **per-chat 进程映射**：`_processes: dict[chat_id, Process]`。
- **lazy spawn**：`acquire(chat_id)` 时如果 `_processes` 没有这个 chat：
  1. 从 storage 读 `sessions.yaml` 取出 `backend / workspace / model / session_id`。
  2. 用 `gateway._create_backend()` 工厂按 backend 实例化（claude / codex / acp）。
  3. `proc.start()`，存入 map。
- **release 不归还到队列**，保留进程给该 chat 复用。
- **idle GC**（可后续做）：超过 N 分钟没活动就 stop+pop。

### 7. Resume API 泛化

`/api/claude/resume` → 同时接受新 payload：

```json
{
  "bot": "raw",
  "backend": "claude" | "codex" | "copilot",
  "session_id": "...",
  "project": "<encoded path>",   // 来自 ~/.claude/projects 或 ~/.codex/sessions
  "machine": "..."
}
```

旧 payload（`bot=<具体 bot>`）保留，向后兼容。

raw 分支的实现：
1. 构造 `chat_id = f"{backend}-{sid}"`（避免不同 backend 的 sid 撞车）。
2. 取 workspace：`claude_native.project_cwd(encoded)`（claude）/ codex 的等价函数
   （新增）。
3. `storage.save_session("raw", sid, chat_id=chat_id, backend=...,
   workspace=..., model="")`。
4. `raw_pool.set_workspace(chat_id, workspace)` /
   `raw_pool.set_session_id(chat_id, sid)`。
5. 返回 `{ok, chat_id, session_id, backend, workspace}`。

### 8. Web UI Resume 弹窗

`src/boxagent/web/static/index.html` + `app.js`：

- "⏱ Resume Claude session…" 按钮改名 "⏱ Resume native session…"。
- 弹窗顶部新增 backend 三选一 radio：claude / codex / copilot（默认 claude）。
- 选 backend 后，project picker 调对应 endpoint：
  - `/api/claude/projects` / `/api/claude/sessions`（已有）
  - 新增 `/api/codex/projects` / `/api/codex/sessions`（解析 `~/.codex/sessions/`）
  - copilot：TBD（如果 copilot 没有可枚举的 native session 目录，可暂不暴露）
- POST `/api/resume` 时 `bot` 固定 `"raw"`，附带 `backend`。
- 侧栏照常显示 `raw` bot；其下 chat 名按 backend 颜色/前缀区分（可选）。

## 不做的事

- 不动 sessions.yaml schema：已有 `backend` 字段够用。
- 不动 `--resume` 行为：仍由 backend 自己拼。
- 不实现完整 codex/copilot 的 project/session 列举（仅 claude 在 v1 启用）；
  codex 列举为 v2 增量。

## 改动量估算

| 模块 | 行数 |
|---|---|
| `config.py` / `agent_env.py` 字段 | ~5 |
| `router/core.py` 守卫 | ~10 |
| `claude_process.py` / `codex_process.py` / `acp_process.py` 守卫 | ~15 |
| `sessions/raw_pool.py`（新） | ~120 |
| `gateway.py` 注册 raw bot + 分支 _start_bot + resume 处理 | ~80 |
| `web/static/index.html` + `app.js` | ~60 |
| 合计 | **~290** |

## 测试计划

1. 启动 BoxAgent，验证 `raw` bot 出现在 web 侧栏，无 Telegram 通道。
2. Resume 一个普通 `claude` 旧 session，发一句话：
   - 抓 `claude` 子进程命令行：**不应**出现 `--append-system-prompt` 和
     `--mcp-config`。
   - 模型回复中**不应**主动调用任何 BoxAgent MCP 工具（telegram、peer 等）。
3. 在 web 里继续聊几轮，验证 chat_id 一直是 `claude-<sid>`，sessions.yaml
   `raw:claude-<sid>` 字段正确。
4. 关闭 BoxAgent，再用纯 `claude --resume <sid>` 在终端打开同一 session：
   - 历史一致，工具集为终端默认（无 BoxAgent 注入残留）。
5. Resume codex session（v2 上线后），验证走 codex backend 而非 claude。
