# 架构总览

> 权威长文见 `docs/current-architecture.md`（4 层结构 + 信息流时序图 + 跨界点分析）
> 与 `docs/codebase-guide.md`（真相版文件地图）。本页是 agent 视角的浓缩。

## 装配根：Gateway

`src/boxagent/gateway.py` 是唯一的 composition root。`main.py` 造一个 `Gateway(config)`
并调 `start()`。**看懂启动顺序 = 看懂系统怎么拼起来的。**

`Gateway.start()` 真实顺序（`gateway.py:141` 起）：

1. **Storage**（`sessions/storage.py`）— session_history.yaml + transcripts。
2. **事件总线装配**（`gateway.py:149-201`）：
   - 造 `EventStore`（SQLite，`events.db`）。
   - 造**一根共享 MessageBus**：单机是 `MessageBus`，配了 `cluster_tunnel` 的节点是 `ClusterBus`（events + chat + rpc 都骑它）。
   - 造 `EventBus(store, bus)`，`log.bind(event_bus)` — 之后业务代码所有 `log.info(...)` 都进这根总线。
   - 挂 `TelegramNotifier`、`RetentionSweeper`、`EventSyncer`（跨机复制）。
3. **Phase 1 建 managers**（`gateway.py:206-240`）：`AgentManager` / `TopologyService` / `RequestReply`（跨机 RPC）/ `ClusterHttpRoutes` / `WebHttpServer`。
4. **Web 先起**（`gateway.py:245`）— 页面在其余启动时就可达。
5. **起所有 bot**（`gateway.py:248`）：`AgentManager.start_all_for_node(node_id)`。
6. **Scheduler**（`gateway.py:251`）+ 其 HTTP 路由。
7. **InternalApiServer**（TCP aiohttp，`/api/schedule/run`，端口写 `api-port.txt`）。
8. **McpHttpServer**（uvicorn streamable-http MCP）。
9. **HostElection**（`gateway.py:271`，仅配了 cluster 时）— 运行时决定 host/guest + failover。

> **两阶段 DI**：Phase 1 用已存在的基础设施建 manager；Phase 2 用 setter 晚绑兄弟引用
> （`set_scheduler` / `set_host_election`）。AgentManager 拥有所有 per-bot 状态 dict，别人按引用读。

## 单 bot 消息全流程（唯一真实信息流）

workgroup 删除后，系统只剩这一条 dispatch 流（源码 `router/core.py`）：

```
Telegram/Web ──IncomingMessage──► Router.handle_message           (core.py:83)
                                        │
             ┌── trusted 或 uid∈allowed_users? ──否──► "Unauthorized" 拒绝   (core.py:96)
             │
             ├── text 以 "/" 开头 且命中 COMMAND_REGISTRY? ──► 命令 handler → return
             │
             ├── pool.get_active(chat_id) 忙? ──► 存入 _pending_messages，return
             │                                    （本轮结束后 _dispatch 合并成
             │                                     "[Messages arrived...]" 追加一轮）
             ▼
        _dispatch(msg) ──► _dispatch_one(msg)                     (core.py:126/165)
             │
             ├─ build_env(msg, router) → AgentEnv          (router/env_builder.py)
             ├─ _build_session_context(env) 拼 append_system_prompt
             │     （passthrough=raw bot 跳过；叠加 _resume_contexts / _compact_summaries）
             ├─ 解析 "@model ..." 前缀 → model_override
             ▼
        async with _acquire_proc(chat_id) as backend:   # pool.acquire→release
             await backend.send(prompt, ChannelCallback(channel, chat_id),
                                model=model_override, chat_id=..., 
                                append_system_prompt=..., env=env)
             callback.on_stream(chunk) ──► channel.stream_update(handle, chunk)
             ▼
        turn_failed = backend.last_turn_failed        # send 不抛异常，读字段判断
        storage.save_session(bot, sid, chat_id=..., preview=..., model=..., workspace=...)
```

**关键事实**（全部来自 `router/core.py`）：

- **全程独占 chat_id、单一 backend、无跨 bot 调用**。`_dispatch` 是"跑一轮 + drain 缓冲消息"的循环，`_dispatch_one` 是单轮。
- `trusted=True` 绕过 `allowed_users`（`core.py:96`）—— 现在只有 cluster 中继消息打这个标记（`transports/base.py:33`）。**中继 sender 无验证**，是 cluster 信任模型的软点。
- `_compact_summaries` / `_resume_contexts` 是 Router 实例的**内存 dict**（`core.py:44-45`），跨进程重启丢失。两者都**只在 turn 成功后消费**（`core.py:241-247`），失败留着给重试。compact 的历史另靠 storage 链式保存持久化，不受内存 dict 丢失影响。

## 模块依赖 DAG（单向，不许退化成网状）

```
Gateway ──┬─ AgentManager ──── per-bot: Router + Backend + Pool + Channel + Watchdog
          ├─ TopologyService ─┐
          ├─ RequestReply      ─┤ cluster 状态 + host↔guest RPC
          ├─ ClusterHttpRoutes ┤
          ├─ HostElection      ┘
          ├─ Scheduler ──────── cron（独立 process spawn）
          ├─ InternalApiServer  内部 aiohttp（/api/schedule）
          ├─ McpHttpServer      uvicorn streamable-http（/mcp/{base,telegram}）
          └─ WebHttpServer      Web UI + cluster guest WS（Starlette + Hypercorn, HTTP/2）

单向 DAG:  history < sessions < router
  - history/ 不依赖任何 boxagent 子包（只读原生 transcript）
  - sessions/ 仅 browser/ 引 history/
  - router/ 引 sessions.{Storage, SessionPool}

解耦契约:
  - Router → Backend  经 AgentBackend Protocol（agent/protocol.py）
  - Router → Channel  经 Channel Protocol（transports/base.py）
  - bus/ 是中立 leaf：不 import 任何项目内模块；events/ 与 cluster/ 都依赖它、彼此不依赖
```

**代码组织铁律**（`docs/vision.md` "代码组织原则"）：Core（agent / router / transports /
sessions / scheduler / watchdog / gateway）**不 import cluster**。Cluster 知道 Core，反过来不行。
既有模块边界是反复重构后的产物，**不要"顺手"动**。

## 两个核心数据契约

- **`IncomingMessage`**（`transports/base.py:24`）：channel / chat_id / user_id / text / attachments / reply_to / **trusted** / timestamp / channel_info。这是 channel → router 的入口契约。
- **`AgentEnv`**（`agent_env.py`）：每条消息生成的 env 快照（channel / chat_id / bot_name / workspace / ai_backend / model / yolo / passthrough 等），**唯一构造点**是 `router/env_builder.py:build_env`。workgroup 删除后已无任何 workgroup 字段。

下一步：[Agent Backends](agent-backends.md) 看 backend 怎么出回复。
