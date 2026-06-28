# BoxAgent 当前架构地图（写于 2026-05-10）

> 这份文档**不提改进方案**，只描述系统**现在实际怎么跑**。
> 用于在动手重构前让我们俩看到同一份地图。

> ⚠️ **新人请注意**：本文件主体（4 层结构图、3 条信息流时序图、Router/AgentEnv 字段清单）是 **2026-05-10 的快照**，之后只加了下方"2026-05-15 增量更新"段、未重写正文。如果要看**当前**真实结构以 `docs/codebase-guide.md` 为准；本文价值在于看 5-10 时点的细节、字段清单、跨界点分析。

> **2026-05-15 增量更新**（不重写全文，只点出主要新增项；权威以 `codebase-guide.md` 为准）：
> - 新增 `boxagent/log/` facade + `boxagent/events/`（EventBus + SQLite EventStore + 跨机 sync + retention + Telegram notifier + web SSE stream）。Gateway 启动时 `bind_event_bus`；HostElection 生命周期里挂 `EventSyncer`。
> - Web UI 多页化：Chat / Events / Schedules，三页统一 top-left 导航；主题系统拆为 shape × palette 两轴；CSS 拆 `style.css` + `events.css` + `*.themes.css`。
> - Cluster：host election 提升前 retry probe 防 split-brain；ClusterTunnel 改用 `devtunnel list -j` 解析跨 region 同名 tunnel，重复时 warn + 选 active。
> - Compact：BUG88/89 修复——session 链式保存 + raw-read jsonl 跨 `/compact` 保留历史；`/compact` prompt 对齐 Claude CLI 结构化格式。
> - SDK Claude：dowhen `<return>` instrumentation 透出 timestamp/cwd/gitBranch/recap；`requires-python` 升 3.12。
> - Backend：`claude-cli` 静默重定向到 `agent-sdk-claude`（旧 config 兼容）。

---

## 1. 四层结构 + 真实依赖方向

```
┌─────────────────────────────────────────────────────────────────┐
│  Gateway (gateway.py)                                           │
│  ── 装配根 ── 持有 manager 们，做 Phase-1/Phase-2 DI            │
│  ── 启动时调 bind_event_bus(EventBus(EventStore))               │
└─────────────────────────────────────────────────────────────────┘
                    │
   ┌────────────┬───┴───────────────┬──────────────┬─────────────┐
   ▼            ▼                   ▼              ▼             ▼
AgentManager WorkgroupManager  TopologyService  PeerService  HostElection
(per-bot)    (admin+specialist) (cluster状态)   (peer 消息)  ＋ EventSyncer
   │            │                   │              │             │
   │            ▼                   │              │             │
   │       启动多个独立的 Router ─┐ │              │             │
   │       + Backend + Pool       │ │              │             │
   ▼                              │ │              │             │
单个 Router ─────────────────────┐│ │              │             │
                                 ▼▼ ▼              │             │
                ┌─────────────────────────────────┐│             │
                │  核心 6 件套                    ││             │
                │  router/  agent/  sessions/     ││             │
                │  transports/ scheduler/ ...     ││             │
                └─────────────────────────────────┘│             │
                                  ▲                │             │
                                  │ workgroup_mgr  │             │
                                  └────────────────┘             │
                                    (setter 注入)                │
                                                                 │
   横切：boxagent.log facade ──► EventBus ──► EventStore (SQLite)│
                                       │                         │
                                       ├─► WebStream (SSE → web) │
                                       ├─► TelegramNotifier      │
                                       └─► EventSyncer ──────────┘
                                           (跨机复制，host↔guest)

iOS app（ios/BoxAgent/）─► Web HTTP/SSE API（与 web UI 同源），不算独立 transport
```

**关键事实**：
- AgentManager 和 WorkgroupManager **平级**，互不知道彼此存在；都被 Gateway 持有
- WorkgroupManager 内部**自己组装** Router/Backend/Pool 三件套（不复用 AgentManager 的）
- TopologyService、PeerService 是 cluster 包的"外露 API"，挂在 Gateway 上
- **EventBus 是横切**：业务代码经 `boxagent.log` 写入，三类订阅者各自消费（web SSE / Telegram / 跨机 sync）。业务代码禁止直接 import `boxagent.events`
- iOS app 不是独立 transport，复用 Web 的 HTTP + SSE 接口

---

## 2. 三条真实信息流

### 流 A：单 bot 普通消息

```
Telegram                                        ClaudeCLI
  │                                                ▲
  ▼                                                │
TelegramChannel.poll() ──IncomingMessage──> Router │
                                              │    │
                                  handle_message()   │
                                              │      │
                          ┌───authorized?──────┤    │
                          │ /command? ─────────┤    │
                          ▼                    │    │
                  pool.acquire(chat_id) ────►  │    │
                  (借 backend，注入 session_id) │    │
                          │                    │    │
                  build_env(msg, router) ──►   │    │
                  build_session_context(env)   │    │
                          │                    │    │
                  backend.send(prompt, callback,  ──┘
                              env, append_system_prompt)
                          │
                  ChannelCallback(channel, chat_id)
                          │
                  ◄── on_stream(chunk) ──── backend
                          │
                  channel.stream_update(handle, chunk)
                          │                    │
                  pool.release(chat_id, proc) ─┘
                  storage.save_session(bot, sid, chat_id=...)
```

**全程独占的 chat_id，单一 backend，无跨 bot 调用**。

### 流 B：workgroup admin → specialist（异步）

```
Web UI                                                Admin ClaudeCLI
  │                                                       ▲
  ▼                                                       │
WebChannel ──IncomingMessage(channel="web")──► AdminRouter
                                                  │
                                       (跟流 A 一样跑一轮)
                                                  │
                                       backend 决定调 send_to_agent MCP tool
                                                  │
                                                  ▼
                                       MCP HTTP /mcp/admin/send_to_agent
                                                  │
                                                  ▼
                                  WorkgroupManager.send_to_specialist(target, text)
                                                  │
                            ┌─────────────────────┼─────────────────────┐
                            ▼                     ▼                     ▼
                  alloc task_id     adapter.post_task()   asyncio.create_task(_run)
                                    (web._publish 把           │
                                    任务消息推到 specialist     │
                                    的 wg:<name> chat 上)       │
                                                                ▼
                                                       SpecialistRouter
                                                       .dispatch_sync(wrapped_text,
                                                              chat_id=wg:<name>,
                                                              from_bot=admin)
                                                                │
                                            构造 IncomingMessage(via_workgroup=True,
                                                              channel="internal")
                                                                │
                                                  跑 Router._dispatch (跳 auth / cmd)
                                                                │
                                                       Specialist ClaudeCLI
                                                                │
                                                                ▼
                                                       <specialist_response>...</>
                                                                │
                                            _extract_specialist_response → result
                                                                │
                              ┌─────────────────────────────────┤
                              ▼                                 ▼
              adapter.notify_admin(reply_chat_id,         AdminRouter.handle_message(
                  "[name] done\n preview")                IncomingMessage(
                              │                              channel="internal",
                              ▼                              text="[TaskResult ...]",
                  Web UI 上展示给 admin 用户                  trusted=True,
                                                             via_workgroup=True))
                                                                │
                                                       admin AI 处理任务结果
```

**关键点**：
- specialist 是**完整独立**的 Router + Backend + Pool（在 WorkgroupManager 里）
- admin → specialist 走的是 MCP 工具调用 → manager 方法 → 异步 task
- specialist → admin 反馈走两条路：①  `notify_admin` 给用户看；② 把结果作为新 IncomingMessage 灌回 admin router 让 AI 自己处理
- **dispatch_sync 这条路径在 Router 上专门为 workgroup 存在**——绕过 auth、绕过 command 解析

### 流 C：跨机 peer message（admin@A → admin@B）

```
admin-a (host 机)                                      admin-b (guest 机)
  │                                                              ▲
  ▼                                                              │
AdminRouter-A 跑一轮                                              │
  │                                                              │
  backend 调 send_to_peer MCP tool                                │
  │                                                              │
  ▼                                                              │
MCP HTTP /mcp/peer/send_to_peer                                  │
  │                                                              │
  ▼                                                              │
PeerService.send_peer(target=admin-b, sender=admin-a, message)   │
  │                                                              │
  ├─ 本地 workgroup_mgr 有没有 admin-b？没有 ───┐                │
  │                                              │                │
  ├─ guest_registry 里找 admin-b 在哪台 guest 上 │                │
  │                                              │                │
  ▼                                              ▼                │
guest_registry.get(machine_id_for_admin_b).call()                │
   │                                                              │
   POST /api/wg/peer/recv {target, sender, body}                  │
   │                                                              │
   通过 host↔guest WebSocket 投递 ─────────────────────────────►  │
                                                                  │
                                                 PeerService(guest 侧)
                                                 .handle_wg_peer_recv()
                                                          │
                                                          ▼
                                              _dispatch_local_peer(target, sender, body)
                                                          │
                                                构造 IncomingMessage(
                                                    channel="internal",
                                                    chat_id=main_chat_id_provider(b),
                                                    text="[Peer message from a]\n...",
                                                    trusted=True)
                                                          │
                                                          ▼
                                              admin_router_b.handle_message(msg) ────┘
```

**注意 `via_workgroup=False`** 这条 peer message——它当成普通用户消息进 admin-b 的 router（没有 via_workgroup 标记！只有 trusted=True）。

---

## 3. 核心数据类的"workgroup/cluster 沾染"

### `IncomingMessage` (transports/base.py:24)

> **更新（2026-06-28，yait #98 Phase 1）**：`via_workgroup` 字段已删除——实测它从不被读来
> 驱动行为（纯空载透传）。下面保留旧描述作对照。

```python
class IncomingMessage:
    channel: str            # "telegram" / "web" / "internal"
    chat_id: str
    user_id: str
    text: str
    attachments: list[Attachment]
    reply_to: str | None
    via_workgroup: bool = False  # ⚠️ 漏：workgroup-specific
    trusted: bool = False        # 用于绕过 auth（workgroup + cluster 都用）
    timestamp: datetime
    channel_info: ChannelInfo | None
```

构造点：
- 真实 channel（Telegram/Web）：`via_workgroup=False, trusted=False`
- `Router.dispatch_sync`（workgroup 调用）：`via_workgroup=True`
- `WorkgroupManager.send_to_specialist` 回调 admin：`via_workgroup=True, trusted=True`
- `PeerService._dispatch_local_peer`（cluster peer）：`trusted=True` 但 `via_workgroup=False`
- `Router._dispatch` 内 drain pending messages：复制原 msg 的 `via_workgroup`

**毛病**：peer message 该不该带 `via_workgroup`？现在不带，但它确实是"非用户输入"。语义不一致。

### `AgentEnv` (agent_env.py:84)

> **更新（2026-06-28，yait #98 Phase 1）**：下方"5 个 workgroup 字段平铺"的描述已过时。
> 现在这些字段已归并进嵌套的 `workgroup: WorkgroupContext | None`（`role` / `agents` /
> `running_tasks` / `peers` / `has_peer_channel`），`is_workgroup_admin` / `is_specialist` /
> `has_peer_channel` 改为 property 委托到它；死字段 `via_workgroup` 已删除。下面保留旧描述
> 作为重构前的对照。

12 个字段里**5 个是 workgroup-specific**：

```python
@dataclass(frozen=True)
class AgentEnv:
    # 通用
    channel, chat_id, user_id, bot_name, display_name, node_id,
    workspace, config_dir, local_dir, telegram_token,
    ai_backend, model, yolo, passthrough

    # ⚠️ workgroup 专属
    via_workgroup: bool = False
    workgroup_role: str = ""
    workgroup_agents: tuple[str, ...] = ()
    running_tasks: tuple = ()
    peers: tuple = ()
    has_peer_channel: bool = False

    @property
    def is_workgroup_admin(self) -> bool: ...
    @property
    def is_specialist(self) -> bool: ...
```

构造点：仅 `router/env_builder.py:build_env`，从 Router 实例字段抄过来。

### `Router` (router/core.py:25)

字段里**5 个是 workgroup-specific**：

```python
@dataclass
class Router:
    # 通用
    backend, channel, allowed_users, storage, pool, bot_name,
    display_name, config_dir, node_id, local_dir, start_time,
    workspace, extra_skill_dirs, ai_backend, on_backend_switched,
    telegram_token, passthrough, _compact_summaries, _resume_contexts,
    _channels, _pending_messages

    # ⚠️ workgroup 专属
    workgroup_agents: list[str] = []
    get_running_tasks: Callable | None = None
    get_peers: Callable | None = None
    has_peer_channel: bool = False
    workgroup_role: str = ""

    # ⚠️ workgroup-only 方法
    async def dispatch_sync(self, text, chat_id, from_bot=""): ...
```

写入点：
- 单 bot：`AgentManager.start_bot`，5 个 workgroup 字段全留默认
- workgroup admin：`WorkgroupManager.start_workgroup`，5 个全填
- workgroup specialist：`WorkgroupManager._create_specialist_agent`，5 个全留默认（specialist 是普通 router，只是被 admin 派活）

**但 specialist 也走 `via_workgroup=True` 的 dispatch_sync 路径**——所以 router_role 不能区分"我是 specialist 还是普通 bot"，得从外部 `from_bot` 字段知道。

### `ChannelCallback` (router/callback.py)

```python
class ChannelCallback:
    webhook_name: str = ""  # bot name for webhook-based workgroup replies
```

只有一个字段、目前没真用。

---

## 4. 跨界点（seam）一览

| 名字 | 物理位置 | 性质 |
|---|---|---|
| `IncomingMessage.via_workgroup` | transports/base.py | 数据类带 workgroup tag |
| `IncomingMessage.trusted` | transports/base.py | workgroup + cluster 共享的"绕 auth"开关 |
| `AgentEnv.workgroup_*`（5 个字段 + 2 properties） | agent_env.py | env 知道 workgroup |
| `Router.workgroup_*`（5 个字段） | router/core.py | router 自带 workgroup 配置 |
| `Router.dispatch_sync` | router/core.py | workgroup 专属入口 |
| `if env.is_workgroup_admin:` | agent/claude_process.py:108 | backend 决定挂哪些 MCP server |
| `from boxagent.workgroup.manager import format_running_tasks` | router/context.py:82 | system prompt 拼接时反向 import workgroup |
| `if workgroup_agents: ...` | router/context.py:81-100 | system prompt 硬编码 workgroup 段落 |
| `if has_peer_channel: ...` | router/context.py:106-114 | system prompt 硬编码 peer 段落 |
| `Router.get_peers / get_running_tasks` | router/core.py | 注入式 callback，cluster 状态从 TopologyService 流入 |
| `PeerService.set_workgroup_mgr` | cluster/peer_service.py:41 | cluster→workgroup 唯一耦合，setter 注入 |
| `TopologyService.set_workgroup_mgr` | cluster/topology_service.py:46 | 同上，cluster 读 workgroup.routers 列表 |
| `WebWorkgroupAdapter._publish / _allocate_id` | workgroup/channel_adapter.py | workgroup 调用 WebChannel 私有方法（绕过 Channel Protocol） |

---

## 5. 几个具体的"奇怪"

### a) chat_id 命名空间是隐式约定

- Telegram bot：chat_id = telegram chat id 数字字符串
- Web bot：chat_id = 由 Web UI 生成的字符串
- workgroup specialist：chat_id = `wg:<specialist_name>` （硬编码前缀）
- workgroup admin 主聊天：`storage.get_or_create_main_chat_id(name)` 返回的合成 id
- `Router._channels` 用 `msg.channel` 字符串做 key（`"web"` / `"telegram"` / `"internal"`）

没有命名空间检查，`wg:` 前缀冲突就靠"约定"。

### b) Router.workgroup_role 的三态

- `""` → 普通 bot
- `"admin"` → workgroup 管理员
- `"specialist"` → workgroup specialist

> **更新（2026-05-10，commit `ab9ab9d`）**：曾经有一段历史是 `"specialist"` 从未被赋值（`_create_specialist_agent` 留默认 `""`），导致 `AgentEnv.is_specialist` 永远返回 False、被视为 dead code。该问题已修：`WorkgroupManager._create_specialist_agent` 现在显式 `workgroup_role="specialist"`（`workgroup/manager.py:193`），`is_specialist` 是活代码。下游 system-prompt / MCP gating 可以放心据此分支。

### c) `via_workgroup` 在 peer message 上是 False

`PeerService._dispatch_local_peer` 构造 IncomingMessage 时只设 `trusted=True`，不设 `via_workgroup=True`。所以如果 admin 收 peer message → 跑一轮 → 调 send_to_agent，整个调用链里**这条 peer message 看起来不像"通过 workgroup 来的"**。

可能没事（admin 收谁的 message 都一样跑），但语义不一致就是 bug 的温床。

### d) WorkgroupManager 反向构造 Router 但不通过 AgentManager

- AgentManager 起普通 bot：`backend → pool → router`，全套自己组
- WorkgroupManager 起 admin/specialist：**完全一样的 4 行**，复制了一遍

两份并列代码，任何 Router 配置变更都得改两处（甚至三处——还有 `start_raw_bot`）。

### e) heartbeat 是又一条 dispatch 入口

`HeartbeatManager` 周期性触发：
- 读 `HEARTBEAT.md` → 构造 prompt → 调用 admin_router 的某种 dispatch（需要再看）
- "fork"主 chat（同一 chat_id 上跑出额外的 turn）

这个我没看完，但**它和 send_to_specialist + peer message 三条入口都灌进 admin router**，如果有 race / state stomp，三条入口都可能是凶手。

### f) cluster 的 `trusted=True` 配合 admin 的 `allowed_users` 检查

`Router.handle_message` 在 trusted=True 时跳过 auth。peer message + workgroup callback 都靠这个。但 `from_bot` / `sender` 是个**纯字符串字段**，没有验证（在跨机 RPC 那条路径上，host 收到 `/api/wg/peer/recv` 直接信任 sender）。

---

## 6. 总览表

| 维度 | 单 bot | workgroup admin | workgroup specialist | cluster peer |
|---|---|---|---|---|
| Router 创建于 | AgentManager | WorkgroupManager | WorkgroupManager | （借用 admin） |
| 入口 channel | telegram/web | web | "internal"（dispatch_sync） | "internal" |
| `IncomingMessage.via_workgroup` | False | False（用户来时）/ True（specialist 回调时） | True | **False** ⚠️ |
| `IncomingMessage.trusted` | False | False / True（回调时） | True | True |
| `Router.workgroup_role` | `""` | `"admin"` | **`"specialist"`**（修于 ab9ab9d，详见 5(b)） |  |
| `AgentEnv.is_workgroup_admin` | False | True | False | True（admin 在收 peer msg） |
| backend MCP servers | base + (telegram?) | base + admin + (telegram?) + peer | base | base + admin + peer |
| chat_id 形态 | telegram int / web uuid | web uuid + main-chat-id | `wg:<name>` | main-chat-id |

---

## 已识别的疑似 bug 风险点（不是确证 bug）

1. **peer message 上 `via_workgroup` 没设**——见 5(c)
2. **~~`AgentEnv.is_specialist` 是 dead code~~** —— 已修于 commit `ab9ab9d`（2026-05-10）：specialist Router 现在显式 `workgroup_role="specialist"`，property 正常返回 True。
3. **`from_bot` / `sender` 跨机投递无验证**——见 5(f)
4. **三条入口（user / specialist callback / peer / heartbeat）都打 admin router 同一 chat_id**，pending message buffer 行为复杂——见流 B + 流 C 都用 `main_chat_id_provider`
5. **`_compact_summaries` / `_resume_contexts` 是内存 dict**，admin router 跨进程重启丢失（你之前提过）
6. **WorkgroupManager 持有的 routers/pools 与 AgentManager 持有的完全分离**，cluster registry 同时枚举两边时容易漏一边

---

地图就到这里。**你现在能告诉我 bug 在哪一格吗？**——比如"admin 收到 peer message 后 reply 不见了"、"specialist 处理完任务 admin 没收到通知"、"两个 admin 互发消息混在一起"等。指到具体格子，下一步就有目标。
