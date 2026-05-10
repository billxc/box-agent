# BoxAgent — 代码库导览

> 给读代码的人看的"真相版"。如果跟代码不一致，**以代码为准**——回来改这份文档。
> 历史决策见 `decisions.md`，远景见 `vision.md`，详细架构图见 `current-architecture.md`。

## 一句话

Telegram / Web UI / iOS / MCP 进来 → Router 鉴权 + 派活 → AgentBackend（Claude CLI / Codex CLI / Agent SDK Claude / Agent SDK Copilot）出回复 → 流式回到原 channel。Gateway 是装配根，Workgroup 是 admin↔specialist 多 bot 编排，Cluster 把多机串起来。

## 顶层结构

```
src/boxagent/
├── main.py                  CLI 入口
├── gateway.py               Gateway 装配根 + InternalApiServer
├── config.py                AppConfig / BotConfig / WorkgroupConfig / SpecialistConfig
├── agent_env.py             AgentEnv / ChannelInfo（每条消息的统一 env 快照）
├── utils.py                 杂项 helpers
├── doctor.py                `boxagent doctor` 子命令
├── watchdog.py              进程死掉自动重启
├── router/
│   ├── core.py              Router（鉴权 / 命令 / dispatch）
│   ├── callback.py          ChannelCallback / TextCollector / log_turn
│   ├── context.py           system prompt 拼接（含 BOXAGENT.md）
│   ├── env_builder.py       IncomingMessage → AgentEnv
│   └── commands/            slash 命令（@command 装饰器自动注册）
│       ├── registry.py      COMMAND_REGISTRY + @command
│       ├── info.py          /status /help /version /verbose ...
│       ├── session.py       /new /cancel /resume /compact /model /cd /backend
│       ├── tools.py         /exec /schedule
│       └── workspace.py     /sessions /trust_workspace /sync_skills
├── agent/
│   ├── protocol.py          AgentBackend Protocol + BACKEND_KINDS
│   ├── backend_factory.py   create_backend() 按 ai_backend 分发
│   ├── agent_manager.py     AgentManager（per-bot 生命周期、watchdog）
│   ├── workspace.py         ensure_git_repo / sync_skills
│   ├── base_cli.py          CLI 类 backend 共享基类
│   ├── claude_process.py    Claude CLI（subprocess）
│   ├── codex_process.py     Codex CLI（subprocess）
│   ├── sdk_claude_process.py   claude_agent_sdk（in-process）
│   ├── sdk_copilot_process.py  GitHub Copilot SDK（in-process）
│   ├── callback.py          AgentCallback Protocol
│   └── mcp_endpoints.py     pick_mcp_endpoints() — 决定哪些 MCP server 挂上去
├── transports/
│   ├── base.py              Channel Protocol / IncomingMessage / Attachment / StreamHandle
│   ├── telegram/            TelegramChannel + md 格式 + 长消息 splitter
│   ├── web/                 WebChannel + WebHttpServer + static 前端
│   └── mcp/                 create_mcp_app + McpHttpServer（streamable-http）
├── sessions/
│   ├── storage.py           Storage（session_history.yaml + transcripts/）
│   ├── base_pool.py         BaseSessionPool（chat_id ↔ backend 绑定）
│   ├── pool.py              SessionPool（预热 N 个 backend 共享）
│   ├── raw_pool.py          RawSessionPool（per-chat 懒生成；raw bot 用）
│   └── browser/             /sessions /resume 浏览器（合并 history + Storage）
├── history/
│   ├── protocol.py          AgentHistory Protocol（只读 transcript adapter）
│   ├── claude.py            读 ~/.claude/projects/
│   ├── codex.py             读 ~/.codex/sessions/
│   ├── copilot.py           读 Copilot SDK 自己的 session
│   └── factory.py           get_history(backend) 分发
├── tools/
│   ├── registry.py          @boxagent_tool 装饰器 + tools_for() / env_capabilities()
│   ├── builtin/             副作用 import 触发注册
│   │   ├── sessions.py      sessions_list
│   │   ├── schedule.py      schedule_list / add / show / run / logs / run_detail
│   │   ├── peer.py          send_to_peer（含 send_to_peer:<name> 动态别名）
│   │   ├── admin.py         workgroup admin 工具（send_to_agent / list_specialists / ...）
│   │   └── telegram_media.py send_photo / send_document / send_video / ...
│   └── adapters/            backend-specific MCP 包装
│       ├── mcp_http.py      registry → FastMCP HTTP（claude-cli / codex-cli）
│       ├── claude_sdk.py    registry → SdkMcpServer（agent-sdk-claude）
│       └── copilot_sdk.py   registry → 原生 Tool 对象（agent-sdk-copilot）
├── cluster/                 多机互联（host ↔ guest WS RPC）
│   ├── registry.py          host: GuestRegistry + GuestSession（含 wire protocol 文档）
│   ├── guest_client.py      guest: GuestClient（拨向 host）
│   ├── host_election.py     主备投票 + failover
│   ├── topology_service.py  本机标识 / peer 描述符 / machine snapshot
│   ├── peer_service.py      send_to_peer 跨机投递（local + RPC fallback）
│   ├── rpc.py               ClusterRpc（host→guest HTTP/SSE 转发）
│   ├── http_routes.py       cluster 路由挂载（/api/guest/ws、/api/peer/send）
│   ├── tunnel.py            host 端 devtunnel 生命周期（spawn / 重启）
│   └── devtunnel.py         devtunnel CLI 包装（resolve url、auth）
├── workgroup/               admin ↔ specialist 多 bot 编排
│   ├── manager.py           WorkgroupManager（spawn admin + specialist routers）
│   ├── channel_adapter.py   Workgroup transport 适配（Web + Null 实现）
│   ├── heartbeat.py         HeartbeatManager（admin 周期性自驱动）
│   ├── persistence.py       workgroup_specialists.yaml 读写
│   ├── task_queue.py        SpecialistTaskQueue（per-target FIFO）
│   ├── template_loader.py   templates/ 发现 + 加载
│   ├── specialist_skills.py 模板 → specialist workspace skills
│   ├── workspace_templates.py admin/specialist workspace seeding
│   ├── http_routes.py       workgroup HTTP 路由（/api/workgroup/*）
│   └── templates/           内置 admin / specialist 模板
├── scheduler/
│   ├── engine.py            Scheduler（cron + isolate/append 两模式）
│   ├── cli.py               `boxagent schedule` 子命令
│   └── http_routes.py       SchedulerHttpRoutes（POST /api/schedule/run）
└── testing/
    └── mocks.py             MockBackend / MockChannel（Channel + AgentBackend 测试 double）
```

## 想读懂的话，按顺序读

**核心 dispatch 链路**：
1. `gateway.py` —— Gateway 装配 8 个 manager；看 `start()` 知道启动顺序
2. `transports/base.py` —— Channel Protocol、IncomingMessage 数据类（这是核心契约）
3. `agent_env.py` —— 每条消息生成的 AgentEnv 快照
4. `router/core.py` —— `handle_message` → `_dispatch_one`，主流程
5. `agent/protocol.py` —— AgentBackend Protocol（4 个 backend 共同接口）
6. `agent/backend_factory.py` —— `create_backend()` 按 `ai_backend` 选实现
7. `agent/claude_process.py` —— 主参考实现（其它三个类似）

**workgroup**：
8. `workgroup/manager.py` —— admin/specialist 创建、`send_to_specialist()` 派活
9. `workgroup/channel_adapter.py` —— 内部消息总线抽象

**cluster**：
10. `cluster/registry.py` 顶部 docstring —— host↔guest wire protocol
11. `cluster/host_election.py` —— host 选举与 failover

**架构总览**：`docs/current-architecture.md` 有 4 层结构图 + 3 条信息流时序图 + 数据类污染分析。

## 模块依赖（高层）

```
Gateway ──┬─ AgentManager ──── per-bot Router + Backend + Pool
          │
          ├─ WorkgroupManager ─ per-workgroup admin + specialists
          │                     （独立装配 Router/Backend/Pool）
          │
          ├─ TopologyService  ─┐
          ├─ PeerService      ─┤ cluster 状态 + 跨机投递
          ├─ ClusterRpc       ─┤
          ├─ ClusterHttpRoutes ┤
          ├─ HostElection     ─┘
          │
          ├─ Scheduler ──────── cron 任务（独立 process spawn）
          ├─ InternalApiServer  内部 aiohttp（/api/peer /api/workgroup /api/schedule）
          ├─ McpHttpServer     uvicorn streamable-http（/mcp/{base,admin,telegram,peer}）
          └─ WebHttpServer     Web UI + cluster RPC 路由

Router → Backend：通过 AgentBackend Protocol 解耦
Router → Channel：通过 Channel Protocol 解耦
Backend → MCP：HTTP 端到 McpHttpServer（claude-cli / codex-cli）
              或 in-process（sdk-claude / sdk-copilot）
sessions/browser → history → 后端原生 transcript 文件
```

**单向 DAG**：`history < sessions < router`。`history/` 不依赖任何 boxagent 子包；`sessions/` 仅 `browser/` 引 `history/`；`router/` 引 `sessions.{Storage, SessionPool}`。

## 三种 backend……不，**四种**

| ai_backend | 进程模型 | session 持久化 | MCP 挂载方式 |
|---|---|---|---|
| `claude-cli` | 每轮 spawn `claude` subprocess | `--resume <session_id>`（Claude SDK 自管） | `--mcp-config` JSON |
| `codex-cli` | 每轮 spawn `codex exec` subprocess | `codex exec resume <session_id>`（Codex 自管） | `-c mcp_servers.X.url=...` 配置 override |
| `agent-sdk-claude` | 长驻 in-process（`claude_agent_sdk.query`） | 跟 claude-cli 共享 ~/.claude/ | `SdkMcpServer` 直接注入 SDK |
| `agent-sdk-copilot` | 长驻 in-process（`CopilotClient`） | 自己管的 session 文件 | 原生 Tool 对象列表 |

session 持久化由 backend 自己负责，BoxAgent 只在 `Storage` 里记 `chat_id → session_id` 绑定。MCP 挂载由 `agent/mcp_endpoints.py:pick_mcp_endpoints()` 统一决定哪些端点（base/admin/telegram/peer）该上，各 backend 用各自的语法落实。

## /sessions 与 /resume 的关系

`/sessions` slash 命令（`router/commands/workspace.py`）和 MCP `sessions_list` 工具（`tools/builtin/sessions.py`）都调 `sessions/browser/format_sessions_list()`，输出合并后的统一列表。`_load_all_unified_sessions()` 把三个数据源 merge：

1. `history.ClaudeAgentHistory` 读 `~/.claude/projects/`
2. `history.CodexAgentHistory` 读 `~/.codex/sessions/`
3. `Storage.list_session_history()` 读 BoxAgent 自己的 `session_history.yaml`

合并键是 `session_id`，BoxAgent 的 yaml 给 Claude/Codex 原生 session **加注解**（backend / model / bot / preview）。

`/resume <id>` 走 `router/commands/session.py:cmd_resume`：写 `pool.set_session_id(chat_id, sid)` + `storage.save_session(...)`。

## 扩展点

### 加 slash 命令

在 `router/commands/` 下新建 .py 文件，用 `@command(name, help, category)` 装饰函数：

```python
from boxagent.router.commands.registry import command, CommandCategory

@command("/foo", help="my command", category=CommandCategory.TOOLS)
async def cmd_foo(router, msg, channel):
    await channel.send_text(msg.chat_id, "hi")
```

`router/__init__.py` 启动时 auto-discover commands 子包，触发装饰器注册。

### 加 MCP 工具

在 `tools/builtin/` 下新建文件，`@boxagent_tool` 注册：

```python
from boxagent.tools import boxagent_tool, ToolContext

@boxagent_tool(
    name="my_tool",
    group="base",                   # base / admin / telegram / peer
    description="...",
    schema={"arg1": str},
    requires=[],                    # ["workgroup_admin"], ["telegram"], ["peer_channel"]
)
async def my_tool(args: dict, ctx: ToolContext) -> str:
    return "result"
```

`tools/builtin/__init__.py` 副作用 import 触发注册。`group` 决定挂在哪个 MCP endpoint，`requires` 决定哪些 env caps（admin / has_telegram / has_peer_channel）才暴露给 backend。

### 加 backend

实现 `agent/protocol.py:AgentBackend` Protocol，在 `agent/backend_factory.py:create_backend()` 加分支，加进 `agent/protocol.py:BACKEND_KINDS`。MCP 挂载这边按 `pick_mcp_endpoints()` 输出自己拼参数（参考 `claude_process.py` JSON 形式或 `codex_process.py` `-c` 形式）。

### 加 channel transport

实现 `transports/base.py:Channel` Protocol（send_text / stream_* / on_tool_* 等）。看 `transports/telegram/channel.py` 或 `transports/web/channel.py` 抄。在 Gateway 启动时挂上去；`Router._channels[name]` dict 按 `IncomingMessage.channel` 字符串路由回复。

## 测试约定

- 单元测试 `tests/unit/test_*.py`，集成 `tests/integration/`（默认 skip）
- **Backend / Channel 的 mock 用 `boxagent.testing.MockBackend / MockChannel`**，不要手搓 AsyncMock：
  ```python
  from boxagent.testing.mocks import MockBackend, MockChannel
  backend = MockBackend(session_id="sess_x")
  backend.script(["chunk1", "chunk2"])  # 脚本化 stream 输出
  channel = MockChannel()
  # ... 断言 backend.sends / channel.sent_texts / channel.streams
  ```
- **黑盒 e2e**：`tests/unit/test_router_e2e.py` 是范本——`channel.deliver(IncomingMessage)` 进，`backend.sends` + `channel.streams` 出，全程不 peek Router 私有状态
- 跑：`uv run pytest -x -q`

## 已知坑

1. **`mcp-port.txt` 偶发被外部清掉**：`claude_process.py` / `codex_process.py` 都靠这个文件 gate 整个 MCP 挂载块，丢了就静默无 MCP。重启 boxagent 重写。
2. **Codex CLI 的 MCP wiring 曾被遗忘**（commit 2aa1ae7→0b9d0a5），现已恢复
3. **`workgroup_role="specialist"`** 字符串在代码里**从来没有人写**——specialist Router 默认 `workgroup_role=""`，`AgentEnv.is_specialist` property 永远 False。设计上 specialist Router 跟普通 Router 不区分（被派活时凭 `IncomingMessage.via_workgroup` 判断）
4. **`_compact_summaries` / `_resume_contexts`** 是 Router 实例的内存 dict，跨进程重启丢失
