# Box-Agent Workgroup vs Isotopes Subagent: Comparison & Lessons

> Date: 2026-04-26
> Source: [isotopes](../../../isotopes) (`@ghostcomplex/isotopes`, TypeScript)

Isotopes 是一个 TypeScript 多 agent 框架，和 box-agent 解决类似问题（多 agent 协作 + 多平台消息路由）。
本文档总结 box-agent workgroup 可以从 isotopes 借鉴的设计。

---

## 架构对比概览

| 维度 | Box-Agent | Isotopes |
|------|-----------|----------|
| 语言 | Python | TypeScript |
| 多 agent 模式 | Workgroup (admin + specialists) | Subagent (parent spawns child) |
| Agent 进程模型 | 独立 CLI 进程 (Claude/Codex/ACP) | 两种：独立进程 (Claude SDK) 或 in-process (builtin) |
| 并发控制 | SessionPool (固定池大小) | MAX_CONCURRENT_AGENTS + rate limiter |
| 委派模式 | 同步 request-response | 异步 event stream |
| 消息路由 | 每 bot 一个 Router | Binding resolution (specificity scoring) |
| 人格定义 | BOXAGENT.md | SOUL.md + MEMORY.md + TOOLS.md |
| 热更新 | 无 (新 session 时读取) | 文件监控 + debounce reload |
| 任务追踪 | 无全局注册表 | TaskRegistry singleton |
| 失败保护 | 无 | FailureTracker (rate limit + consecutive failure block) |
| 权限控制 | workspace 隔离 | per-agent tool guards (allow/deny list) |

---

## 1. 并发控制与失败追踪

### Isotopes 做法

`SubagentBackend` (`src/subagent/backend.ts`):
- 硬限制 `MAX_CONCURRENT_AGENTS = 5`，超出排队等待
- 每个 run 有 `AbortController`，支持随时取消
- 所有 runs 的 handle 存在全局 `Map<string, RunHandle>`

`FailureTracker` (`src/subagent/failure-tracker.ts`):
- 按 session 追踪 spawn 频率：5 次 / 5 分钟窗口内即 block
- 按 task key 追踪连续失败次数：2 次即 block 该任务
- 记录 cancel 历史，防止被取消的任务被重新 spawn

### Box-Agent 现状

- `send_to_specialist()` (`gateway.py:692-730`) 同步阻塞等待返回，无超时
- 无并发限制——admin 如果串行委派尚可，但无机制防止过载
- 无失败追踪：specialist 反复失败时 admin 可能无限重试

### 建议

- 给 `dispatch_sync()` 加 timeout 参数（默认 5 分钟）
- 实现简单的 `FailureTracker`：记录 specialist 连续失败次数，超过阈值返回错误而非继续调用
- 考虑并发委派：admin 可以同时给多个 specialist 发任务（`asyncio.gather`）

---

## 2. Task Registry（任务可观测性）

### Isotopes 做法

`TaskRegistry` (`src/subagent/task-registry.ts`):

```typescript
class TaskRegistry {
  private tasks: Map<string, TaskInfo>;  // taskId → metadata
  
  register(taskId, sessionId, channelId, task)
  unregister(taskId)
  getBySession(sessionId): TaskInfo[]
  getByThreadId(threadId): TaskInfo | undefined
  list(): TaskInfo[]
}
```

- 全局单例，追踪所有运行中的 subagent task
- REST API `GET /api/subagents` 暴露 running tasks
- Discord `/stop` 命令按 threadId 查找并取消任务

### Box-Agent 现状

- workgroup delegation 无全局注册表
- 无法从外部查看哪些 specialist 正在工作
- Scheduler 有 `running_tasks` dict，但 workgroup 没有类似机制

### 建议

在 `gateway.py` 加一个简单的 `_active_specialist_tasks: dict[str, TaskInfo]`：
- `send_to_specialist()` 开始时 register，结束时 unregister
- HTTP API `GET /api/workgroup/tasks` 暴露
- 为未来的 `/cancel <specialist>` 打基础

---

## 3. Binding-based 消息路由

### Isotopes 做法

`bindings.ts` (`src/core/bindings.ts`):
- 用 (channel, accountId, peer) 三元组匹配 agent
- Specificity scoring：`channel+accountId+peer > channel+accountId > channel`
- 同一 Discord bot 可以按 guild/thread/DM 路由到不同 agent

### Box-Agent 现状

- 每个 bot token 对应一个 Router，消息直接发到对应 Router
- Workgroup 内部靠 specialist name 显式路由
- 不支持"同一 bot 按群聊路由到不同 agent"

### 建议

当前架构已经够用。但如果未来需要：
- 同一 Telegram bot 在不同群里表现为不同角色
- 同一 Discord bot 按 category/channel 路由

可以参考 binding 模式。在 `config.yaml` 中定义路由规则：
```yaml
bindings:
  - agent: researcher
    match: { channel: telegram, peer: { kind: group, id: "-100123456" } }
  - agent: assistant
    match: { channel: telegram }  # fallback
```

---

## 4. Hot-Reload（workspace 文件热更新）

### Isotopes 做法

`HotReloadManager` (`src/workspace/hot-reload.ts`):
- 监控文件：`SOUL.md`, `IDENTITY.md`, `MEMORY.md`, `TOOLS.md`, `skills/**/*.md` 等
- Debounce 500ms 后调用 `agentManager.reloadWorkspace(agentId)`
- 自动重建 system prompt，不需要重启

### Box-Agent 现状

- `BOXAGENT.md` 在 `context.py:build_session_context()` 中读取
- 只在每次 session 开始时加载，修改后需要新 session 才生效
- 配置变更需要重启 gateway

### 建议

简单方案：在 `build_session_context()` 中每次重新读取 BOXAGENT.md（去掉 cache）。
进阶方案：用 `watchdog` 库监控 BOXAGENT.md 变化，触发 context 刷新。

---

## 5. Subagent Event Streaming

### Isotopes 做法

Subagent 返回 `AsyncIterable<SubagentEvent>`：

```typescript
type SubagentEventType = "start" | "message" | "tool_use" | "tool_result" | "error" | "done";

interface SubagentEvent {
  type: SubagentEventType;
  content?: string;
  toolName?: string;
  toolInput?: unknown;
  toolResult?: string;
  error?: string;
}
```

三路 fan-out：
1. **Discord sink** — 实时显示 subagent 在 thread 中的输出
2. **Recorder** — 持久化到 SessionStore（audit trail）
3. **TaskRegistry** — 追踪运行状态

### Box-Agent 现状

- `dispatch_sync()` 只返回最终 response string
- Admin agent 看不到 specialist 的中间过程（tool_use、思考过程）
- Specialist 的 Discord channel streaming 已部分实现（通过 `ChannelCallback`）

### 建议

- 给 `dispatch_sync()` 加可选的 `event_callback` 参数
- 保存 specialist 完整对话历史用于 audit
- Admin 如果需要，可以接收 specialist 的中间事件

---

## 6. Backend 抽象：In-Process 轻量模式

### Isotopes 做法

两种 subagent backend：
- **ClaudeRunner**：独立进程 (Claude SDK `query()`)，完全隔离
- **BuiltinRunner**：in-process，复用父 agent 的 `PiMonoCore` LLM 连接

Builtin 模式优势：
- 无进程 spawn 开销
- 共享 API 连接/认证
- 适合简单、快速任务

### Box-Agent 现状

- Specialist 总是独立 CLI 进程（`BaseCLIProcess` 子类）
- 每次调用 spawn 一个新 subprocess
- 启动开销较大（CLI init + session restore）

### 建议

对于简单任务（查资料、格式化、翻译），可以直接调用 LLM API 而不是 spawn CLI：
- 新增一个 `APIProcess` backend，直接发 HTTP request
- 比 CLI process 快得多，适合 workgroup 内部的轻量级委派

---

## 7. Per-Agent Tool Guards

### Isotopes 做法

```yaml
agents:
  researcher:
    tools:
      cli: false        # 不允许执行命令
      fs:
        workspaceOnly: true  # 只能读写 workspace
      web: true         # 允许网络访问
```

- `createToolGuard()` wrap 每个 tool call，检查权限
- 权限说明注入到 system prompt
- Subagent 有 `permissionMode`：skip / allowlist / default

### Box-Agent 现状

- Specialist 通过 `ai_backend` + `workspace` 配置隔离
- 没有 per-specialist tool 级别控制
- 所有 specialist 共享同一 backend 的完整 tool set

### 建议

在 `SpecialistConfig` 中加：

```python
@dataclass
class SpecialistConfig:
    # ... existing fields ...
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
```

通过 system prompt 注入权限限制（不需要改 backend，靠 LLM 遵守即可）。

---

## 实施优先级

| 优先级 | 改进项 | 工作量 | 价值 | 说明 |
|--------|--------|--------|------|------|
| **P0** | Delegation timeout | 小 | 高 | 防止 specialist 调用无限等待 |
| **P0** | Task Registry | 中 | 高 | 可观测性，知道谁在做什么 |
| **P1** | Failure Tracker | 小 | 中 | 防止无限重试失败的 specialist |
| **P1** | 并发委派 | 中 | 中 | admin 同时给多个 specialist 派任务 |
| **P2** | Event streaming | 中 | 中 | admin 看到 specialist 过程 + audit |
| **P2** | Per-specialist tool guards | 小 | 中 | 安全边界更细粒度 |
| **P3** | Hot-reload BOXAGENT.md | 小 | 低 | 调试便利 |
| **P3** | In-process 轻量 backend | 大 | 中 | 降低简单任务开销 |
| **P3** | Binding-based routing | 大 | 低 | 未来扩展方向 |
