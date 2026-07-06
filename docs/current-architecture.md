# BoxAgent 当前架构地图（写于 2026-05-10，2026-06-30 移除 workgroup 后重写）

> 这份文档**不提改进方案**，只描述系统**现在实际怎么跑**。
> 用于在动手重构前让我们俩看到同一份地图。

> ⚠️ **新人请注意**：本文价值在于看细节、字段清单、跨界点分析。若要看**当前**真实结构的总览，仍以 `docs/codebase-guide.md` 为准。
>
> **2026-06-30 重写说明**：`src/boxagent/workgroup/` 整个模块（WorkgroupManager / HeartbeatManager / PeerService / channel_adapter / prompt_fragment 等）已从代码库**完全删除**。随之删除的还有：`tools/builtin/admin.py` + `tools/builtin/peer.py`、`AgentEnv` 的全部 workgroup 字段、`Router.dispatch_sync` + workgroup 字段、gateway 的 `install_workgroup` / `/api/workgroup/*` / `/api/peer/send`、`config.py` 的 `SpecialistConfig` / `WorkgroupConfig` / `AppConfig.workgroups`、`topology_service` 的 peer-descriptor / workgroup-provider 相关方法、`guest_client` 的 `remote_peers`、`storage` 的 `main_chat_id` 相关方法、`log/categories` 的 `HEARTBEAT_*`。本文已据此重写：删掉了原 §2 的「流 B（admin→specialist）」与「流 C（跨机 admin peer）」两条信息流、原 §3 的 workgroup 沾染分析、原 §4 seam 表里的 workgroup 行。**现在"peer"在 cluster 层只表示一台 peer *机器*（cluster topology），不再有 workgroup admin peer 概念。**
>
> **其它增量（历史，权威以 `codebase-guide.md` 为准）**：
> - `boxagent/log/` facade + `boxagent/events/`（EventBus + SQLite EventStore + 跨机 sync + retention + Telegram notifier + web SSE stream）。Gateway 启动时 `bind_event_bus`；HostElection 生命周期里挂 `EventSyncer`。
> - Web UI 多页化：Chat / Events / Schedules / Logs，统一 top-left 导航；主题系统拆为 shape × palette 两轴；CSS 拆 `style.css` + `events.css` + `*.themes.css`。
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
   ┌────────────────┼────────────────────┬─────────────┐
   ▼                ▼                    ▼             ▼
AgentManager   TopologyService     HostElection   (cluster registry)
(per-bot)      (cluster 机器状态)  ＋ EventSyncer  GuestRegistry
   │                                                  ▲
   ▼                                                  │
单个 Router ──────────────────────┐                  │
                                  ▼                  │
                ┌─────────────────────────────────┐ │
                │  核心 6 件套                    │ │
                │  router/  agent/  sessions/     │ │
                │  transports/ scheduler/ ...     │ │
                └─────────────────────────────────┘ │
                                                     │
   横切：一根共享 MessageBus（events + chat 同实例）
   boxagent.log facade ──► EventBus ──► bus.publish("events.<cat>")
                                       │   （下面都是 events.* 的 subscriber）
                                       ├─► StoreSubscriber ──► EventStore (SQLite, 第一/同步)
                                       ├─► WebStream (SSE → web)
                                       ├─► TelegramNotifier
                                       └─► EventSyncer ──► PeerTransport ──► 跨机复制(host↔guest)

   WebChannel._publish ──► bus.publish("chat.<machine>.<bot>.<chat>")  （同一根 bus，chat.* 无 StoreSubscriber → 永不进库）
                                       ├─► 浏览器 SSE queue (local)
                                       └─► ChatSyncer ──► PeerTransport ──► 跨机订阅(host↔guest)

iOS app（ios/BoxAgent/）─► Web HTTP/SSE API（与 web UI 同源），不算独立 transport
```

**关键事实**：
- AgentManager 负责 per-bot 生命周期：每个 bot 自己组装 Router/Backend/Pool 三件套
- TopologyService、HostElection、GuestRegistry 是 cluster 包的"外露 API"，挂在 Gateway 上。**TopologyService 现在只描述机器级拓扑**（machine descriptors + machines_snapshot），不再有 peer/workgroup 描述符
- **一根 content-agnostic MessageBus（`bus/`）横切**：events(`events.*`) 和 chat(`chat.*`) 同一个实例，同步保序 fan-out。持久化/广播是 subscriber 行为（EventStore 只订 `events.`，所以 chat 永不进库）。跨机由 EventSyncer(broadcast) / ChatSyncer(demand) 两个 sibling 共用 `PeerTransport` 发帧；RPC 骑同一 transport（`rpc_over_bus`，request/reply）。业务代码经 `boxagent.log` 写入，禁止直接 import `boxagent.events`
- iOS app 不是独立 transport，复用 Web 的 HTTP + SSE 接口

---

## 2. 单 bot 普通消息（唯一真实信息流）

> 原文档这里有三条流：流 A（单 bot）、流 B（workgroup admin→specialist）、流 C（跨机 admin peer）。后两条随 workgroup 模块删除已不存在，下面只剩单 bot 这一条。
>
> 跨机的 RPC（web 端拉别机的 sessions/history、send/stream 中继）仍然存在，走 `ClusterRpc` + guest WS + `dispatch_machine_*`，但那是 **web 联邦读写**，不是某个 agent 主动给另一台机器的 agent 发消息——不要把它当成被删掉的"peer message 流"的替代品。

```
Telegram                                        ClaudeSDK backend
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

**全程独占的 chat_id，单一 backend，无跨 bot 调用**。`build_env` 只从 Router 的通用字段构造 `AgentEnv`（无 workgroup 字段）；`handle_message` 在 `trusted=True` 时才跳过 auth（cluster 中继的消息会带这个标记）。

---

## 3. 核心数据类（workgroup 沾染已清除）

workgroup 模块删除后，原本被它"沾染"的核心数据类已经回归干净。这里只记录现状，不再保留旧的字段污染分析。

### `IncomingMessage` (transports/base.py)

```python
class IncomingMessage:
    channel: str            # "telegram" / "web" / "internal"
    chat_id: str
    user_id: str
    text: str
    attachments: list[Attachment]
    reply_to: str | None
    trusted: bool = False        # 用于绕过 auth（现在仅 cluster 中继用）
    timestamp: datetime
    channel_info: ChannelInfo | None
```

- `via_workgroup` 字段**已删除**（生前就是纯空载透传，从不驱动行为）。
- `trusted` **保留**：cluster 把别机/中继来的消息标 `trusted=True` 让 `Router.handle_message` 跳过 `allowed_users` 检查。这是它现在唯一的用途。

### `AgentEnv` (agent_env.py)

`AgentEnv` 现在**不含任何 workgroup 字段**。原来平铺/嵌套的 `workgroup_role` / `workgroup_agents` / `running_tasks` / `peers` / `has_peer_channel` / `via_workgroup` 以及 `is_workgroup_admin` / `is_specialist` / `has_peer_channel` property、`heartbeat_display_mode` 全部删除。剩下的都是通用字段（channel / chat_id / user_id / bot_name / display_name / node_id / workspace / config_dir / local_dir / telegram_token / ai_backend / model / yolo / passthrough 等），构造点仍仅 `router/env_builder.py:build_env`。

### `Router` (router/core.py)

`Router` 现在只做 **auth / slash 命令 / dispatch**。原来的 workgroup 字段（`workgroup_agents` / `get_running_tasks` / `get_peers` / `has_peer_channel` / `workgroup_role`）与 **`Router.dispatch_sync` 方法**全部删除。所有 bot（含 cluster guest 上的 bot）都用同一套通用字段，由 `AgentManager.start_bot` 装配，不再有"单 bot vs workgroup admin/specialist"的字段分叉。

### `ChannelCallback` (router/callback.py)

```python
class ChannelCallback:
    webhook_name: str = ""  # bot name for webhook-based bus replies
```

`webhook_name` **保留**（注释已从"workgroup replies"改为"webhook-based bus replies"），目前是 bus 回复用的轻量标识。

### Backend Protocol：`supports_fork` / `fork_and_send`

`AgentBackend` Protocol 上仍保留 `supports_fork` 能力位与 `fork_and_send` 方法（历史上 heartbeat fork 主 chat 用过）。workgroup 删除后**没有调用方**，但接口保留在 Protocol 里——属于"留着没用但不碍事"的死能力，不要顺手删（也别据此 fabricate 新用法）。

---

## 4. 跨界点（seam）一览

workgroup 删除后，原表里所有 workgroup seam（`via_workgroup` 标记、`AgentEnv.workgroup_*`、`Router.workgroup_*` + `dispatch_sync`、`context.py` 的 `[Workgroup]`/`[Peer]` 段、`PeerService.set_workgroup_mgr`、`TopologyService` 的 workgroup-provider、`WebWorkgroupAdapter` 绕 Channel Protocol 等）已随代码一起消失。当前剩下的是 cluster 自己的真实跨界点：

| 名字 | 物理位置 | 性质 |
|---|---|---|
| `IncomingMessage.trusted` | transports/base.py | cluster 中继消息绕 auth 的开关（现在只剩 cluster 一个用户） |
| `cluster/http_routes.py` 仅 `/api/guest/ws` | cluster/http_routes.py | host 侧只剩这一个 cluster wire 端点 |
| `GuestRegistry` ↔ `guest_client` 反向 RPC | cluster/registry.py / guest_client.py | guest 注册到 host 后，host 经 WS 反向调 guest（web 联邦读写 sessions/history/send/stream/schedules 走这条） |
| `TopologyService` machines_snapshot | cluster/topology_service.py | 机器级拓扑快照，host↔guest 同步；已无 peer/workgroup 描述符 |
| `ClusterRpc` + `dispatch_machine_*` | cluster/ | web 端按 machine_id 把请求路由到目标机器（本机 in-process / 跨机经 host 中继） |

---

## 5. 几个具体的"奇怪"

### a) chat_id 命名空间是隐式约定

- Telegram bot：chat_id = telegram chat id 数字字符串
- Web bot：chat_id = 由 Web UI 生成的字符串
- `Router._channels` 用 `msg.channel` 字符串做 key（`"web"` / `"telegram"` / `"internal"`）

（原来 workgroup 还会用 `wg:<name>` / 合成 main-chat-id 占用这个命名空间，现已不存在。）没有命名空间检查，前缀冲突仍靠"约定"。

### b) cluster 的 `trusted=True` 配合 `allowed_users` 检查

`Router.handle_message` 在 `trusted=True` 时跳过 auth。现在只有 cluster 中继路径会打这个标记。注意中继来源标识是个**纯字符串字段**、没有验证——host 收到中继请求后直接信任 sender。这是 cluster 信任模型的一个软点，不是 bug，但跨机 auth 时要记着。

### c) `_compact_summaries` / `_resume_contexts` 是内存 dict

Router 上这两个仍是进程内内存 dict，跨进程重启会丢。compact 的**历史**已经靠 storage 链式保存 + raw-read jsonl 持久化（BUG88/89），不受这个内存 dict 丢失影响；但 resume context 等内存态仍是重启即丢。

---

## 已识别的疑似 bug 风险点（不是确证 bug）

1. **cluster 中继 sender 无验证**——见 5(b)；host 信任任何标了 `trusted=True` 的中继消息的来源字段。
2. **`_compact_summaries` / `_resume_contexts` 是内存 dict**，router 跨进程重启丢失——见 5(c)。

---

地图就到这里。workgroup 已经整体下线，现在系统回到"单用户 / 多机 / 多 bot，但每个 bot 独立、agent 之间不再互相派活"的形态。若要找 bug，先定位是哪台机器的哪个 bot 的哪个 chat_id，再沿单 bot 流（§2）追调用链。
