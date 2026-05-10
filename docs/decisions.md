# 决策日志

记录每次偏离原始设计的决定，以及原因。

> **路径漂移说明**：本文件是历史日志。条目里提到的文件路径反映**决策当时**的代码布局；后来许多文件改名/搬迁/合并（如 `cluster/cluster_rpc.py` → `cluster/rpc.py`、`gateway/http_api_server.py` → `gateway.py`、`channels/` → `transports/`、`sessions/claude_native.py` → `sessions/browser/loaders.py`）。当前代码以 `codebase-guide.md` 为准。

---

## 2026-05-08: 完全删除 Discord 支持

**决定**: 删除 `channels/discord.py`、所有 BotConfig/WorkgroupConfig 里的 `discord_*` 字段、agent_env 里的 `discord_*` ChannelInfo、workgroup admin 的 Discord category/webhook 路径、`discord.py>=2.0` 运行时依赖、专门测试与 `docs/workgroup-discord-dependency.md`。

**原因**: Owner 决定单人自用场景下 Discord 维护成本高且不好用，用 Telegram + Web/iOS 已经覆盖所有日常使用。继续保留两套渲染（webhook/category 路由 vs Telegram MarkdownV2）只是负债。

**应用**: `channels.discord` / `discord_bot_id` / `transport: discord` 等遗留 yaml 字段会被静默忽略——不会报错也不再生效；只走 Telegram 和 Web。Workgroup admin 之间走 cluster RPC（已有），specialist 走虚拟 chat_id `wg:<name>`。

---

## 2026-03-22: 删除 max_workers 和 display.streaming

**决定**: 从 BotConfig 中删除 `max_workers` 和 `display_streaming` 字段。

**原因**: BoxAgent 定位是轻量桥接，不做并发管理（backend 自己处理），所以 worker pool 不会实现。`display.streaming` 虽然能解析但运行时没有消费，Telegram 始终流式输出。与其留着死代码误导人，不如删掉，需要时再加。

---

## 2026-03-22: 文档归档

**决定**: 将所有早期设计文档移入 `docs/archive/`，只保留反映当前实现的文档。

**原因**: 项目从设计到实现过程中大幅收敛，早期文档（V1 设计、V2 路线图、实现计划）与实际代码不一致，容易误导维护者。`codebase-guide.md` 已经准确描述了当前状态。

**实际处理**: 当时这批文件其实是**直接删除**而非归档（`docs/archive/` 目录此后才在 2026-05-10 创建）。下面列表是当时被删的文件清单，仅作历史记录：

- `2026-03-20-boxagent-design/` — 初版英文设计
- `2026-03-20-boxagent-design.zh-CN/` — 初版中文设计
- `plans/2026-03-20-boxagent-v1/` — V1 实现计划（10 个文件）
- `2026-03-20-boxagent-v1-implementation.md` — V1 实现回顾
- `2026-03-21-boxagent-v2-design.md` — V2 设计 + 路线图
- `2026-03-22-codex-recovery-fix.md` — Codex 恢复修复分析
- `boxagent-vision-vs-current.drawio` — 愿景 vs 现状对比图

**保留的文档**:
- `README.md` — 入口
- `codebase-guide.md` — 代码库导读（现状文档）
- `decisions.md` — 本文件

---

## 2026-03-22: 需求收敛记录

以下是原始设计中提出但未实现的功能，以及当前判断：

| 功能 | 原始设计 | 当前状态 | 判断 |
|------|----------|----------|------|
| Web UI Channel | V1 设计 | ✅ 已实现 | 2026-05-02 落地，独立端口 9292 + cluster 联邦 |
| Git 同步管理 (SyncManager) | V1 设计 | 未实现 | 冻结 — 单机够用 |
| LiteLLM / API Backend | V1 设计 | 未实现 | 冻结 — claude-cli + codex-acp 够用 |
| 自定义 Python Backend | V1 设计 | 未实现 | 冻结 |
| 知识库与偏好系统 | V1 设计 | 未实现 | 冻结 |
| CLIProcessPool (多 worker) | V1 设计 | 未实现 | 冻结 — isolate scheduler 暂够用 |
| Rate Limiting | V2 路线图 | 未实现 | 想要 — 防跑飞 |
| Conversation Logging | V2 路线图 | ✅ 已实现 | JSONL per session |
| Skill Registry | V2 路线图 | 未实现 | 冻结 — symlink 够用 |
| Structured JSON Logging | V2 路线图 | 已实现 | ✅ |
| Scheduler | V2 路线图 | 已实现 | ✅ |
| display.streaming 配置 | 配置已解析 | ✅ 已删除 | 2026-03-22 清理 |
| PID 跟踪 | 辅助代码存在 | ✅ 已删除 | 2026-03-22 清理 |
| max_workers 配置 | 配置已解析 | ✅ 已删除 | 2026-03-22 清理 |

---

## 2026-03-22: 文档与代码对齐

**决定**: 全面审查 usage-guide、codebase-guide、status、decisions，清理已删除功能的残留引用，补充新实现的 transcript 功能。

**变更**:
- usage-guide: 删除 `display.streaming` 配置说明和示例、PID 目录引用，新增 transcripts 目录
- codebase-guide: 删除 PID 跟踪章节和 display.streaming 章节，新增 Transcript 章节，更新死代码章节
- status: 重写已知 bug 部分（区分已修复/未修复），更新功能对照表
- decisions: 更新需求收敛表格中 4 项的状态

**原因**: 项目快速迭代 3 天，一天内删了 3 类死代码 + 加了 transcript，文档和代码已严重不一致。Owner 需要一个可信的文档作为理解代码的入口。

---

## 2026-03-22: 愿景新增 WebView2 集成

**决定**: 在愿景中加入 WebView2 集成方向。

**原因**: BoxAgent 可以兼做 WebView2 宿主应用——既提供桌面端 AI 对话界面（Web UI channel 的一种实现），又能验证 WebView2 功能，一石二鸟。

---

## 2026-05-02: 落地 Web UI channel

**决定**: 实现 Web UI channel — vanilla HTML/CSS/JS（无 build step），独立 aiohttp 端口（默认 9292），mobile-first 单页应用。

**原因**: Owner 显式要求，覆盖了 AGENTS.md 之前"Web UI 已冻结"的判断。Telegram/Discord 在桌面浏览不方便，需要一个跨设备的本地优先 chat 界面。

**关键设计**:
- `WebChannel` 是 Channel 协议第三种实现，per-`chat_id` 用 `asyncio.Queue` fan-out，浏览器通过 SSE 订阅。
- Web UI server 跟内部 `/api/schedule/run` API 分两个端口（不混淆 internal vs UI）。
- 鉴权：localhost 直放 + `web_token` (bearer/query) + `X-BoxAgent-Trusted` header（给反向代理）。
- 默认开启（`channels.web: false` 才关），不会影响 Telegram/Discord 已有路径。

---

## 2026-05-02: Session ID 跨 /compact 链式保存

**决定**: `Storage.save_session` 检测到 chat_id 切换 sid 时，把旧 sid append 到 `previous_session_ids`（截断 20）。`/api/history` 走链合并多份 transcript JSONL。

**原因**: Claude / Codex CLI 在 `/compact` 后会发出新 session_id，原本 BoxAgent 直接覆盖 sessions.yaml 的 sid，旧 transcript 文件就成孤儿，web UI 历史显示不全。

**已知遗憾**: Resume 别人之前 compact 出来的原生 Claude session 时，那段更早的历史 Claude 自己 JSONL 里没存 parent 关系，无从恢复。

---

## 2026-05-02: Claude 原生 session 浏览 + 恢复

**决定**: 新增 `sessions/claude_native.py`，扫 `~/.claude/projects/*/`，按项目分组 + 懒加载列出全部原生 session。Web UI sidebar 提供 "Resume Claude session..." 选择器，选中后 host BoxAgent 把对应 `session_id` + 原始 cwd workspace 写到 `sessions.yaml` 下 `bot:claude-<sid>`，next turn 时 Claude CLI 用 `--resume` 接续。

**原因**: 用户想从 web UI 接管以前在终端裸跑 Claude CLI 留下的对话。

**关键修复**: 项目目录名 `-Users-xiaocw-code-box-agent` naive `-`→`/` 替换会得到错误路径 `/Users/xiaocw/code/box/agent`，导致 `claude --resume` 找不到 session。改成读 JSONL 里 `cwd` 字段拿到真路径。

---

## 2026-05-02: Hub-and-spoke 集群架构

**决定**: 一台机器为 host（自动 `devtunnel create + host`），其余作为 guest WS 主动 dial 进来；host web UI 联邦显示所有节点的 bot，用户选远端 bot 时由 host 通过 RPC over WS 转发到对应 guest。

**原因**: 替代 Discord 作为机器间互通底层；要求"一个浏览器管所有机器"。Hub-and-spoke 比 peer-to-peer 简单：只一个公开端点，guest 在 NAT/防火墙后不需要暴露端口。

**关键设计**:
- 配置只在共享 `config.yaml` 顶层 `cluster: {host, tunnel_name, token}`，每台机器靠自己 `node_id` 自动决定角色，**不需要任何 local 配置或手动回填**。
- Devtunnel 创建**不带 `-a`**，默认认证 → 同 Microsoft 账号才能 mint connect token。Guest 启动时 `devtunnel token --scopes connect` 现场拿 JWT，放 `X-Tunnel-Authorization` header。
- 三道安全门：devtunnel JWT（账号级）+ `cluster.token`（hello frame）+ `web_token`（HTTP 层）。
- WS RPC 协议是通用 envelope（`rpc` / `rpc_resp` / `rpc_stream` / `rpc_end`），host 几乎不用维护 per-endpoint 转发逻辑；SSE 经 guest 拆 `data:` 行 → host 拼回。

**未做**：specialist 跨机调度（思路未理清，已撤回未提交的初版）。

---

## 2026-05-02: 配置文件如何分共享 vs 本地

**决定**: 跨机器共享的配置（cluster 拓扑、bots 定义、workgroup 定义）放共享 `config.yaml`；机器自身身份（`node_id`）放 `local.yaml`。机器角色（host/guest）由共享配置 `cluster.host` 与本地 `node_id` 比对自动得出，避免角色信息散落到本地。

**原因**: 早期把 `guest_token` / `host_url` 都放共享 config 会让每台机器都觉得自己是 host；放本地 `local.yaml` 又破坏了"共享配置即真相"的原则。最终方案：拓扑共享，身份本地，角色派生。

## 2026-05-03: Cluster RPC 入站路由必须挂在 web UI app（不是内部 API app）

**决定**: 凡是 `guest_client` 通过 WS RPC 转发回本机 HTTP 的端点（当前是 `/api/wg/peer/recv`，未来类似端点同理），都必须注册到 `_start_web_http()` 创建的 `wapp` 上，而**不是** `_start_http()` 创建的 `app`。

**原因**: gateway 跑两个独立 aiohttp Application：`app` 在内部 API 端口（动态分配），`wapp` 在 web UI 端口（默认 9292）。`guest_client` 用 `local_web_port` 转发 RPC（gateway.py:282 传的是 `web_port`），所以入站只命中 `wapp`。把路由放 `app` 会让每条跨机 peer 消息**沉默 404**。

**配套**: `Gateway.send_peer` 必须检查 `GuestSession.call(...)` 返回的 `status` 字段（404/500 都是合法 round-trip，不抛异常）。仅 transport 成功不等于业务成功，应把非 2xx 当失败上抛，避免 admin AI 收到 "Message sent" 但对端从未收到。

**回归测试**: `tests/unit/test_cluster_peer_e2e.py::test_peer_recv_route_registered_on_web_app_not_api_app` + `::test_send_peer_surfaces_404_from_sat_recv`。


## 2026-05-03: Workgroup peer discovery comes from cluster registry, not peers.yaml

**决定**: 删掉 `~/.boxagent/peers.yaml` 的读取路径。Workgroup admin 在 system context 里看到的 peer 列表，由 `Gateway._build_peer_descriptors(exclude=self_name)` 动态生成，源 = `_workgroup_mgr.routers`（本机 workgroup-kind） + `_guest_registry.list_bots()`（远端 workgroup-kind） + `_guest_registry.history`（离线带 history）。

**原因**: yaml 是手维护静态副本，跟动态 cluster 状态分叉：
- 加/删 guest 要跨机同步 yaml；不同步就有 admin "看不见" peer 或 "看到不存在" 的 peer
- 没有在线/离线状态
- 同时维护两套真相，cluster registry 里早已知道一切

新链路：`Router.get_peers` callable → `AgentEnv.peers` tuple → `build_session_context` 渲染。WorkgroupManager 通过 `_peer_provider` 钩子拿 Gateway 的 helper。

**已知不足**: guest 节点的 `_guest_registry` 是 None — 只能看到本机 workgroup，看不到 host 上的或其他 guest 上的 workgroup。需要 guest→host 反向 RPC 查询补齐（yait #67）。

## 2026-05-09 — Gateway 8 mixin → 显式组合 / 两阶段 DI（yait #86，进行中）

**问题**: Gateway 是 8 个 mixin 经 MRO 拼成的 god-class，每个 mixin 用 `self.X` 隐式访问 Gateway 字段、互相调用对方方法，IDE 跳不过去，依赖关系藏在 self 命名空间里。

**方案**: 8 个 mixin 一对一拆成 8 个 manager 类；依赖通过两阶段注入：
1. **Phase 1（构造器）**: 接收基础设施（config / Storage / 共享 dict）。共享 dict 传引用，不拷贝，原 mixin 的读点（其他文件里的 `self._backends` 等）零改动。
2. **Phase 2（setter）**: 兄弟 manager 在装配阶段通过 `set_xxx()` 注入，解循环依赖（Scheduler 在 bot 起完后才创建）。
3. **Phase 3（start_*）**: Gateway 驱动每个 manager 的生命周期。

**进度**: 第 1 个完成 — `BotsMixin` → `AgentManager`。**没保留 shim**：直接删掉 mixin、Gateway 不再继承 `BotsMixin`、`_GatewayCore.start()` 调 `self._bots.start_bot()`。原本 `test_gateway.py` patch `gw._start_bot` 的 5 处改成两种新写法：(a) class-level `patch("boxagent.agent.manager.AgentManager.start_bot", autospec=True)` —— 用来在 `gw.start()` 期间拦截；(b) 直接调用的 3 个 test 通过 helper `_agent_mgr_from(gw)` 显式构造 `AgentManager`，调用其 `start_bot/restart_bot`。迁就测试是反模式 —— shim 本来就要在最后一个 commit 删，提前一步而已。

**接下来**: PeerService → ClusterRpc → ClusterHttpRoutes → WorkgroupHttpRoutes → HttpApiServer → WebHttpServer，各自独立 commit + 全量绿。

**第 2 个完成 — `TopologyMixin` → `TopologyService`**：构造时只接 `config + web_channels`；`set_workgroup_mgr` / `set_host_election` 两个 setter 解循环依赖（workgroup_mgr 在 bots 后建、host_election 在最后建）。Gateway.start() 装配顺序：`_topology = TopologyService(...)` → bots 起完 → 建 workgroup_mgr 时把 `topology.build_peer_descriptors` 当 `_peer_provider` 注入 → `topology.set_workgroup_mgr(...)` → 建 HostElection 时同样把 `topology.on_topology_change` / `topology.local_bot_descriptors` 当 callback → `topology.set_host_election(...)`。callable 都是绑定方法，所以 topology 在 setter 调用前已是稳定对象，HostElection 触发回调时 topology 已经看见 host_election，不会出现"半初始化"窗口。19 个外部调用点（web/server.py 14、cluster/rpc.py 2、tests 2、core 3）批量改成 `self._topology.X` —— 没保留 underscore shim。

**第 3 个完成 — `PeerMixin` → `PeerService`**：构造时接 `topology + main_chat_id_provider`（callable，避开把整个 Gateway 灌进来 / `_get_or_create_main_chat_id` 在 core）；`set_workgroup_mgr` setter。HTTP 路由注册改成 `self._peer.handle_peer_send` / `handle_wg_peer_recv`（同时存在 cluster/routes.py + gateway/http_api.py 两处 register —— 后者只注册 send，是历史包袱，等 ClusterHttpRoutes / HttpApiServer 重构时一并清理）。`tools/builtin/peer.py` 的 `ctx.gateway.send_peer(...)` 改成 `ctx.gateway._peer.send_peer(...)` —— 这是 MCP 工具直接读 gateway 的少数几处之一，将来如果 gateway 换 facade 这条线要再适配。

**第 4 个完成 — `ClusterRpcMixin` → `ClusterRpc`**：单阶段 DI（只依赖 TopologyService —— 后者已在 Phase 1 建好）。文件名 `cluster/cluster_rpc.py`，类 `ClusterRpc`。`dispatch_machine_request` / `dispatch_machine_stream` / `handle_guest_ws` 三个公共方法（drop underscore）；私有 `_proxy_*` 改成接收 `guest_client/sess` 显式参数（之前是 `self.guest_client`）。web/server.py 9 处 + routes.py 1 处批量改 `self._cluster_rpc.X`。删 `cluster/rpc.py`。

**第 5 个完成 — `ClusterRoutesMixin` → `ClusterHttpRoutes`**：超薄类（只一个 `register(web_app)` 方法）。ctor 接 `peer + cluster_rpc`（两者都已 Phase 1 建好）。文件 `cluster/cluster_http_routes.py`。web/server.py 原本 `getattr(self, "_register_extra_web_routes", None)` 的 defensive lookup 改成显式 `if self._cluster_routes is not None: self._cluster_routes.register(...)` —— 不再依赖 mixin 暗中混入的方法。

**第 6 个完成 — `WorkgroupApiMixin` → `WorkgroupHttpRoutes`**：8 个 handler + 1 个内部 `_schedule_run_bg` helper。Phase 1 ctor 接 `config + config_dir`（schedule.yaml 路径需要）；Phase 2 setter `set_workgroup_mgr` / `set_scheduler` —— scheduler 在 bot 后才建，workgroup_mgr 同期建。文件 `workgroup/workgroup_http_routes.py`。http_api.py 8 处路由注册批量改 `self._workgroup_routes.handle_X`。

**第 7 个完成 — `HttpApiMixin` → `HttpApiServer`**：单阶段 DI（`config + config_dir + local_dir + peer + workgroup_routes + mcp_gateway_context`，全部 Phase 1 已就绪）。文件 `gateway/http_api_server.py`。public 方法 `start()` / `stop()` / `start_mcp()` / `stop_mcp()` —— 内部状态（`_runner` / `_mcp_server` / `_mcp_task`）从 Gateway 搬走，core 不再持有 `_http_runner` field；`api_port_file` / `mcp_port_file` 也成 HttpApiServer 自己的 property。MCP 创建时 `gateway=self` 改成 `gateway=self._mcp_gateway_context`（仍传整个 Gateway 当根 context，但参数名直白）。core.start/stop 调 `await self._http_server.start() / .stop() / .stop_mcp()`。test_gateway.py 改 5 处 patch 到 `HttpApiServer.start` class-level + 3 处直接调用通过 helper `_http_server_from(gw)` 显式构造。

**第 8 个完成 — `WebServerMixin` → `WebHttpServer`**：最大的一个（742 行 → class 化）。Phase 1 ctor 接 `config + local_dir + storage + web_channels + pools + session_meta_cache + topology + cluster_rpc + cluster_routes`（9 个显式依赖）；Phase 2 setter `set_workgroup_mgr`（仅 `/api/claude/resume` 用一次，找 specialist pool）。22 个 `_handle_X` route 方法保留下划线（视为 private impl）；公共生命周期 `start()` / `stop()` / `_register_routes()`。handler 内部 `self.guest_registry` / `self.guest_client` 改成 `self.topology.guest_registry` / `.guest_client` —— 不再依赖原本 `_GatewayCore` 的 property，topology 是单一真相源。**Gateway 不再继承任何 mixin** —— `class Gateway(_GatewayCore)`。test_gateway.py 5 处 `patch.object(gw, "_start_web_http")` 改 class-level patch on `WebHttpServer.start`。

**总结**: 8 个 mixin → 8 个 manager 全部完成。Gateway 继承链从 9 层（8 mixin + _GatewayCore）变成 1 层（_GatewayCore）。每个 manager 的依赖在构造或 setter 中显式声明，不再有 `self.gateway.X` 反查；横向依赖通过 setter 解循环。Gateway 的 `start()` 现在是一份可读的装配清单，先 build managers 再 wire setter。

**遗留**: yait #87（WorkgroupHttpRoutes / SchedulerHttpRoutes 的 wiring 应该回到自己模块内，Gateway 不该 new + setter workgroup 内部组件）。

## 2026-05-09 — Workgroup/Scheduler routes wiring 内化（yait #87）

**问题**: yait #86 第 6 步留的尾巴 — Gateway.start() 自己 new `WorkgroupHttpRoutes` 并 setter 注入 manager + scheduler；`handle_schedule_run` 又是 scheduler 的事，混在 workgroup routes 里只是因为原 mixin 是杂烩。

**改造**:
1. **`scheduler/scheduler_http_routes.py`** 新文件，class `SchedulerHttpRoutes`，单阶段 ctor 接 `config + config_dir + scheduler`。`_start_scheduler()` 建 Scheduler 后顺手 `self._scheduler_routes = SchedulerHttpRoutes(...)`。
2. **`WorkgroupManager.routes`** lazy property — 第一次访问时 `WorkgroupHttpRoutes(workgroup_mgr=self)`。Gateway 不再 new、不再 setter。
3. **`WorkgroupHttpRoutes`** ctor 缩到只接 `workgroup_mgr`，砍掉 `set_workgroup_mgr` / `set_scheduler`、砍掉 `handle_schedule_run` / `_schedule_run_bg`。
4. **`HttpApiServer`** 构造从 Phase 1 移到 `_start_scheduler()` 之后（所有 deps 就绪），ctor 接 `workgroup_routes=(workgroup_mgr.routes if workgroup_mgr else None)` + `scheduler_routes`。`start()` 里 `if self.workgroup_routes is not None:` 才注册 7 条 workgroup 路由 —— 顺带修了"无 workgroup 配置时调 /api/workgroup/* 会 AttributeError 而非 404"的潜在 bug。

**Gateway.start() 现在关于 workgroup 的代码**: 只有 `WorkgroupManager(...)` 那一行 + 3 处 setter 给别的 manager（topology / peer / web_server，这些是别的 manager 的内部依赖，不是 workgroup 的 wiring）。

**测试**: 824 passed

## 2026-05-09 — Gateway core 进一步瘦身（yait #86 续）

延续 #86 的"职责归位"思路，扫了一遍 core.py 又干了 6 件事：

1. **`AgentManager.stop()`**：channels / web_channels / backends（含 session save）/ pools / watchdog tasks 的 teardown 全归 AgentManager —— 它本来就是这些 dict 的 owner。Gateway.stop() 里关于 bot 资源的 ~30 行变 1 行 `await self._bots.stop()`。
2. **`AgentManager.build_scheduler_refs()`**：`_start_scheduler()` 里走三个 dict 拼 `BotRef` 的逻辑搬过去。Gateway 一行 `bot_refs=self._bots.build_scheduler_refs()`。
3. **`Storage.get_or_create_main_chat_id()`**：原本 `_GatewayCore._get_or_create_main_chat_id()` 全是 storage 操作，挪到 Storage。PeerService 直接拿 `self._storage.get_or_create_main_chat_id` 当 callable 用。
4. **删 `_GatewayCore.guest_registry / guest_client / cluster_tunnel` 三个 property**：所有读点之前都通过 `self._topology.guest_registry` 访问了，core 这边的 property 已经无用。
5. **删 `_api_port_file / _mcp_port_file / _web_port_file / _clear_http_artifacts`**：HttpApiServer 自己有同名 property + `_clear_artifacts`。`test_clear_http_artifacts_removes_stale_sock` 改成直测 HttpApiServer。
6. **module + Gateway class docstring 更新**：去掉对已删除字段的引用。

测试: 826 passed (新增 3 个 AgentManager 单测：stop / 错误吞噬 / build_scheduler_refs 跳过 raw)。

src/ 净 -37 行（核心收益在 core.py：-127/+45 = 净 -82）。Gateway.stop() 现在按职责清晰分层：listening ports → host election → scheduler → bots (resources) → workgroup。

**未做（#7）**：WorkgroupManager 接受 `_create_backend / _ensure_git_repo / _sync_skills` 三个 callable —— 改成直接 import 会破 5 处 test_workgroup_integration.py 的 patch 注入点。注入模式本身没坏，只是看起来啰嗦，留给后续。

## 2026-05-09 — 抽出 backend_factory + workspace 模块

`_create_backend` / `_ensure_git_repo` / `sync_skills` 原来挤在 `agent_manager.py`，prefix `_` 是历史遗留（mixin 时代和 BotsMixin 同住）。但它们既不绑定 AgentManager 实例，也被 WorkgroupManager 当 callable 通过 Gateway 转手注入 —— Gateway 完全不该知道这种实现细节。

**拆分**:
- `boxagent/agent/backend_factory.py` — `create_backend(bot_cfg, sid)`，按 `ai_backend` 分发实例化。`ClaudeProcess` 仍走 `boxagent.gateway.ClaudeProcess` 间接查找以保留 `patch("boxagent.gateway.ClaudeProcess")` 的测试钩子。
- `boxagent/agent/workspace.py` — `ensure_git_repo(workspace)` + `sync_skills(workspace, dirs, backend)`。Backend-aware 但不绑特定类，仍归 `boxagent/agent/`（不是 `utils/` 因为知道 `.claude` vs `.agents` 的 BoxAgent 协议）。

**连带**:
1. `agent_manager.py` 直接 import 这俩，方法体里 `_create_backend(...)` → `create_backend(...)`、`_ensure_git_repo` → `ensure_git_repo`。
2. `WorkgroupManager` 删 3 个 callable 字段（`_create_backend`/`_ensure_git_repo`/`_sync_skills`），改成 module 顶 `from boxagent.agent.backend_factory import create_backend`、`from boxagent.agent.workspace import ensure_git_repo, sync_skills`。`if X and self._foo:` 这种 None-guard 全删（函数永远存在）。
3. `Gateway` 构造 WorkgroupManager 时砍 3 个 callable 参数 —— 只剩 `_peer_provider`（这是真正需要注入的 topology bound method）。
4. `specialist_skills.apply_template_skills()` 砍 `sync_skills` 参数，自己 import。
5. **测试 mock 风格升级**：原本 `mgr._create_backend = MagicMock(...)` 把实例属性当 patch 钩子，重构后失效。改成 `with patch("boxagent.workgroup.manager.create_backend", return_value=...)` —— 标准 module-level patch。`test_workgroup_integration.py` 用 autouse fixture 一次性盖住所有 test；`test_workgroup_web_e2e.py` 把 `_make_manager` 改成 `@contextlib.contextmanager` 版本，调用方 `with _make_manager(tmp_path) as (manager, fakes):` 自动覆盖 patch 生命周期。

**测试**: 826 passed

## 2026-05-09 — _GatewayCore 不再当"共享 state holder"

**问题**: 8 个 dict/list 字段挂在 _GatewayCore 上（_channels / _backends / _pools / _routers / _watchdogs / _watchdog_tasks / _web_channels / _session_meta_cache），用法分析后发现：

- 5 个（_channels / _backends / _routers / _watchdogs / _watchdog_tasks）只有 AgentManager 写和读，**根本没共享**
- 1 个（_session_meta_cache）只有 WebHttpServer 用
- 2 个（_pools / _web_channels）才是真共享 —— AgentManager + WorkgroupManager 写、TopologyService + WebHttpServer 读

挂在 _GatewayCore 上是 mixin 时代的化石（`self.X` 必须在共享祖先上）。

**改造**:
1. AgentManager 自己 allocate 7 个 dict/list；ctor 不再接 7 个 dict 参数，只剩 `config + config_dir + storage + start_time`。
2. WebHttpServer 自己 allocate `session_meta_cache`，ctor 砍掉这个参数。
3. _GatewayCore 删 8 个 field（连同 `WebChannel`/`Router`/`SessionPool`/`Watchdog` 4 个 import 也清掉，type 注解都没了）。
4. Gateway.start() 装配阶段，需要共享的两个 dict 通过 `bots.web_channels` / `bots.pools` ref 显式从 AgentManager 借出，传给 TopologyService / WebHttpServer / WorkgroupManager。所有权语义清晰：AgentManager 是 owner，其他人是 reader。
5. test_agent_manager.py + test_gateway.py 配合更新；test 里原本 `gw._backends["bot"] = X` 这种"绕过 start() 直接塞 Gateway 状态"的做法不再可行，改成 `mgr.backends["bot"] = X` 操作 manager 私有 state。

**测试**: 826 passed
**LOC**: src/ 净 -21 行（core.py 净 -18，AgentManager +5，WebHttpServer -2）

## 2026-05-09 — Pyright 接入 + 全量类型清理（225→0）

加 `[tool.pyright]` 配置进 pyproject.toml，basic mode。**初始 225 个错误全部修完**，分 7 个 commit：

| Pass | 文件 | 余下 |
|---|---|---|
| 1 | router/core.py | 220→160 |
| 2 | workgroup/manager.py | 160→139 |
| 3 | transports/telegram/channel.py | 139→112 |
| 4 | transports/web/server.py + router/commands.py | 112→56 |
| 5 | router/callback / agent_manager / scheduler / watchdog | 56→40 |
| 6 | base_cli / topology / heartbeat / registry | 40→24 |
| 7 | doctor / sessions / cluster / SDK adapters | 24→0 |

**核心 pattern**：
- **`object` placeholder 类型** 是 mixin god-class 时代的化石。改成正确的 `Storage | None` / `Channel | None` / `AgentBackend` / `Callable[..., ...]` 后，cascading attribute-access 错误一片消失。
- **`callable(x)` narrow 不工作**，pyright 看到的还是 `object`；改成 `if x is not None` 配合显式 Callable 类型立刻 narrow。
- **`bot = self._bot if self._bot else ...`** 不能持续 narrow；改用 `@property` 一次性 raise-if-None 把整个类内部都"已经 narrow"。
- **真 bug 浮出来的**：
  - `web/server.py` 的 `_authorized` / `_unauthorized` 方法名跟调用方对不上（mixin 重构遗漏），所有未授权请求会 AttributeError。
  - `router/context.py` 的 `ai_backend` / `model` kwargs 完全没用 —— 死参数活了。
  - `sessions/cli/commands.py:sessions_list` 调 `load_config()` 缺必填参数 + 把 AppConfig 当 dict 用。
  - Telegram `download_file(file.file_path, ...)` 没检查 file_path 可能是 None。

**额外发现**：Telegram channel 的 `_bot: Bot | None` + 17 处 `self._bot.X` 早就有 hidden race，重构成 `@property bot` 后干净了。

**测试**: 826 passed（无回归）。`uv run pyright src/` 现在 0 errors。
