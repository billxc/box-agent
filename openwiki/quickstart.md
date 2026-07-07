# BoxAgent openwiki — 快速上手

> 给**未来接手这份代码的 AI agent**（以及人类）看的导航层。
> 每一页都是对应领域的浓缩 + 指路，深水区细节链接到 `docs/` 下的权威文档。
> **代码与文档冲突时以代码为准**，回来改这里。

## 一句话

Telegram / Web UI（含 iOS app）/ MCP 进来 → **Router** 鉴权 + 派活 → **AgentBackend**（Claude / Codex）出回复 → 流式回到原 channel。**Gateway** 是装配根，**Cluster** 把多台机器串成一张网，一根 **MessageBus** 横切 events + chat。

BoxAgent 自己**不做 agent 逻辑**（tool calling / RAG / 记忆），全部交给 Claude CLI / Codex / SDK backend；BA 只负责**编排、桥接、跨机互联**。

## 四层结构

```
   Telegram   Web UI / iOS app   MCP clients        ← 外部交互
       ↓            ↓                 ↓
   Transports (channels): telegram / web / mcp      ← transports/
       ↓
   Router (per-bot): 鉴权 / slash 命令 / dispatch    ← router/
       ↓
   AgentBackend (3 实现): codex-cli /                ← agent/
       agent-sdk-claude / agent-sdk-copilot
       （claude-cli 已静默重定向到 agent-sdk-claude）

   横切 / 装配：
   ├─ Gateway ────── 装配根（composition root）       ← gateway.py
   ├─ Cluster ────── host 选举 / devtunnel / 跨机 RPC ← cluster/
   ├─ Sessions ───── 持久化 + backend pool            ← sessions/
   ├─ Scheduler ──── cron（isolate / append）         ← scheduler/
   └─ MessageBus ─── events + chat 一根总线           ← bus/ + events/ + log/
```

## 各页导航

| 页 | 讲什么 | 什么时候读 |
|----|--------|-----------|
| [架构总览](architecture.md) | 四层结构、Gateway 装配根、单 bot 消息全流程、模块依赖 DAG | 第一次接手，先读这页 |
| [Agent Backends](agent-backends.md) | AgentBackend Protocol、3 种 backend、AgentManager 生命周期 + watchdog、sessions 持久化、scheduler | 改 backend / session / 定时任务 |
| [Transports](transports.md) | Channel Protocol、Telegram、Web UI（server + 前端 + 主题）、MCP、iOS | 改收发消息 / Web UI / 加 channel |
| [Cluster 多机互联](cluster.md) | host/guest 选举、devtunnel、registry ↔ guest_client、topology、跨机 request/reply | 改多机、devtunnel、跨机联邦读写 |
| [Bus 与事件系统](bus-and-events.md) | MessageBus/Packet、ClusterBus 路由、log facade、EventBus/EventStore、跨机 sync | 写事件日志 / 改跨机传输 |
| [扩展点](extending.md) | 加 slash 命令 / MCP 工具 / backend / channel，tools registry，config 结构 | 加新功能前必读 |
| [开发流程](development.md) | 迭代工作流、测试约定（MockBackend/MockChannel）、命名规范、已知坑、yait | 动手写代码前 |

## 跑起来 / 测试

```bash
# 启动（config 目录默认 ~/.boxagent，local 目录 ~/.boxagent-local）
uv run boxagent --config ~/.boxagent/config.yaml

# 全量单测（集成测试默认 skip）
uv run pytest -x -q

# 单个文件
uv run pytest tests/unit/test_router_e2e.py -x -q

# 环境体检
uv run boxagent doctor          # 只检查
uv run boxagent doctor --fix     # 自动装缺的依赖
```

- 入口：`src/boxagent/main.py`（argparse → `Gateway(config).start()`）。
- 子命令：`schedule`（定时任务 CLI，无 daemon）、`doctor` / `install`。
- Python ≥ 3.12。包管理用 `uv`，**不要用 pip**。

## 与 `docs/` 的分工

本 openwiki 的每一页都**按当前源码核对写成**，是给 agent 的 code-grounded 导航层。
`docs/` 是给人类看的较长文档，**迭代快时会滞后于代码**——当作背景/延伸阅读，冲突时**以代码（和本 wiki）为准**：

- `docs/codebase-guide.md` — 文件地图与模块职责（背景参考）
- `docs/current-architecture.md` — 4 层结构 + 信息流时序图 + 跨界点分析
- `docs/decisions.md` — 决策记录 + 踩坑账（"为什么这样"最有价值的一份）
- `docs/bus-protocol.md` — Packet / ClusterBus 协议定稿
- `docs/vision.md` — 远景（**参考，不是指令**；仍提到已删除的 workgroup，勿据此实现）

> ⚠️ **文档可能过时**。`docs/vision.md` 还在讲已删除的 **workgroup 模块**（2026-06-30
> commit `61f256a` 整体删除）。当前形态是"单用户 / 多机 / 多 bot，每个 bot 独立、agent 之间
> 不互相派活"。**任何结论以 `src/boxagent/` 源码为准。**
