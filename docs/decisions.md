# 决策日志

记录每次偏离原始设计的决定，以及原因。

> **路径漂移说明**：本文件是历史日志。条目里提到的文件路径反映**决策当时**的代码布局；后来许多文件改名/搬迁/合并（如 `cluster/cluster_rpc.py` → `cluster/rpc.py`、`gateway/http_api_server.py` → `gateway.py`、`channels/` → `transports/`、`sessions/claude_native.py` → `sessions/browser/loaders.py`）。当前代码以 `codebase-guide.md` 为准。

---

> 📦 **2026-03 ~ 2026-05 的历史条目已归档**到 [archive/decisions-2026-03-to-05.md](archive/decisions-2026-03-to-05.md)。本文件保留 2026-06 起的决策。

## 2026-06-28 — 物理删除 ClaudeProcess（claude CLI subprocess backend）

接 2026 早期"`claude-cli` 静默重定向到 `AgentSDKClaude`"的迁移，本次把 CLI 子进程实现 `claude_process.py` 物理删除（之前只是占位不实例化）。

**调研发现**：删之前 `ClaudeProcess` 还有一处生产实例化 —— `agent_manager.py` 的 raw passthrough bot 用它当 Router 的占位 backend（Router.backend 不能为 None，`env_builder` 无条件读 `.yolo`/`.model`，但真正干活走 pool）。

**改动**：
- `agent_manager.py`：占位 stub 改用 `self._raw_backend_factory(backend="claude-cli", ...)` —— 和 pool 用同一个 factory，产出真正的 `AgentSDKClaude`，删掉 `from ...claude_process import ClaudeProcess`
- 删 `src/boxagent/agent/claude_process.py`（240 行）
- `_normalize_usage` 此前 claude_process / sdk_claude_process 各有一份（生产只用 SDK 那份），删 claude 那份，`test_session_info` 改用 `AgentSDKClaude._normalize_usage`（@staticmethod）
- 删 `test_claude_process.py`（21 例，测 CLI stream-json 解析/`_build_args`）+ `tests/integration/test_cli_real.py`（4 例，真 CLI）+ `test_system_prompt.py::TestClaudeSystemPrompt`（5 例，测 `--append-system-prompt` 拼接）—— 都是 CLI 专属死代码
- `test_workgroup`/`test_agent_backend_protocol` 的 ClaudeProcess 引用改指 `AgentSDKClaude`

**测试数下降说明**：本次测试从 1073 → 1047（-26）。这是删除死代码连带删其测试的**合理例外**，非隐藏回归。Claude backend 的存活实现由 `test_sdk_claude_process.py`（9 例）覆盖，未丢核心覆盖。

**为什么现在删**：`backend_factory` 早已不经 CLI 路径；保留只增加"4 个 backend"的认知负担和误导（测试里有人会 patch 错对象）。

## 2026-06-28 — workgroup 隔离为可插拔模块（路线 B）Phase 1：数据类归并（yait #98）

**背景**：`IncomingMessage` / `AgentEnv` / `Router` 三个核心数据类各自背着一批 workgroup/peer 专属字段，让单机单 agent 主链路读起来背着用不到的概念。Owner 决策不删 workgroup（日常在用），走**路线 B 务实隔离**：把 workgroup 知识尽量收回 workgroup 包，core 只留少量守卫分支，最终目标是删 workgroup = 删包 + 拔 ~5 个守卫（而非外科手术）。详见 yait #98 伞形 issue。

**Phase 1 改动**（本 commit）：
1. **删死字段 `via_workgroup`**：实测它在 4 处被写、0 处被读驱动行为（`is_specialist`/`is_workgroup_admin` 用的是 `workgroup_role`）。从 `IncomingMessage` + `AgentEnv` + env_builder + core.py(×2) + manager.py 整条删除。
2. **AgentEnv 归并**：`has_peer_channel` + `workgroup_role` + `workgroup_agents` + `running_tasks` + `peers` 五个裸字段 → 单一 `workgroup: WorkgroupContext | None`。`is_workgroup_admin`/`is_specialist`/`has_peer_channel` 改为 property 委托到 `workgroup`。

**为什么保留 property 作为读 API**：`agent/mcp_endpoints.py`、`tools/registry.py` 都通过 property 读 → 0 改动。真正要改的只有写入侧（env_builder、heartbeat）和读裸字段的 context.py。

**为什么 env_builder 的 workgroup 守卫用 `role or has_peer_channel or agents`（不含 running_tasks/peers）**：role/peer/agents 是身份字段，running_tasks/peers 是动态状态。普通 bot 的 `get_running_tasks` 是 None，不会误建 WorkgroupContext。

**Router 不动**：Phase 1 只归并 AgentEnv（被传遍 dispatch 链的痛点）。Router 是装配期对象，污染影响面小，留待后续。

**测试**：新增 `tests/unit/test_agent_env_workgroup.py`（6 例：property 委托 + 字段已删）。存量 6 个测试文件改构造点为嵌套形式。全量 1079 passed（基线 1073 + 6）。

## 2026-06-28 — workgroup 隔离 Phase 2 + 3（yait #98）

接 Phase 1（数据类归并），继续把 workgroup 知识从 core 收回 workgroup 包。

**Phase 2 — system-prompt 片段外移**：`router/context.py` 不再硬编码 `[Workgroup]`/`[Peer Messaging]` 段，也不再 `import workgroup.formatting`。渲染逻辑（含 `_format_peer`）搬进 `workgroup/prompt_fragment.py: build_workgroup_block()`；context.py 缩成一个 `if workgroup_agents or has_peer_channel:` 守卫委托。输出逐字节不变。

**Phase 3a — gateway 装配外移**：`WorkgroupManager` 的构造 + `set_workgroup_manager`(topology/peer/web) + start 搬进 `workgroup/wiring.py: install_workgroup(gateway, storage)`。gateway 18 行装配块 → 3 行守卫调用。`WorkgroupManager` import 降级到 TYPE_CHECKING + 字段注解字符串化（gateway 无 `from __future__ annotations`）。

**Phase 3b — peer 条件化构造（方案 A）**：`cluster/peer_service.py` → `workgroup/peer_service.py`。PeerService 改为**仅 `config.workgroups` 时构造**（`gateway._peer` 否则为 None）。配套：
- `ClusterHttpRoutes.register()` 拆分：`/api/guest/ws` 永远挂（核心 cluster），`/api/peer/*` 仅 `peer is not None` 时挂
- `InternalApiServer.peer` 改 Optional，`/api/peer/send` 条件注册
- `send_to_peer` 工具加 `_peer is None` 防御守卫

**为什么 peer 构造守卫留在 gateway cluster 阶段（而非 install_workgroup 内）**：peer 路由必须在 `web_server.start()` 之前注册（aiohttp 启动后 router 冻结），而 install_workgroup 在其后。所以构造点必须前置，用 `if config.workgroups` 守卫保证条件化。

**达成**：删 `workgroup/` 包后，core/cluster 运行时无 import 崩溃（gateway 的 workgroup 运行时 import 全在守卫块内；cluster 对 workgroup 只剩 TYPE_CHECKING）。peer 消息能力随 workgroup 一起出现/消失。

**测试**：新增 test_workgroup_prompt_fragment.py（5）+ test_peer_conditional_wiring.py（2）；peer 3 个测试改 import 路径。全量 1086 passed（基线 1073 + 13）。

## 2026-06-28 — topology→workgroup 依赖反转（yait #98 Phase 5）

接 Phase 1-4，处理最后一处**真实的 cluster→workgroup 运行时耦合**：`TopologyService` 此前通过 `set_workgroup_manager` 持有 `WorkgroupManager`，在 `build_peer_descriptors` / `push_peers_snapshot_to_sats` 里读 `workgroup_manager.routers` 拿"本机活跃 workgroup admin 名字"。

**反转**：
- 删 `TopologyService.set_workgroup_manager` + `self.workgroup_manager` + TYPE_CHECKING 的 `WorkgroupManager` import
- 改为 `set_local_workgroup_provider(callable)` —— 一个返回本机活跃 workgroup admin 名字列表的回调
- `workgroup/wiring.py` 在 install_workgroup 里注册 `lambda: list(manager.routers.keys())`
- `config.workgroups`（core 的 AppConfig 字段）读取保留 —— 那是 config 字段访问，非 workgroup 包依赖

**效果**：cluster 层不再 import / 持有 `WorkgroupManager`。依赖方向掰正：不再是"cluster 设施反向依赖 workgroup"，而是"workgroup 启动时把自己的 bot 名字注册进 cluster"。`cluster/` 对 workgroup 现在只剩 `http_routes.py` 一处 TYPE_CHECKING 的 `PeerService` 类型注解（无运行时依赖）。

**为什么不连 config.workgroups 一起反转**：`config.workgroups` 是 AppConfig（core）的字段，topology 读它是读 config，不是依赖 workgroup 包。强行也反转会把 `local_bot_descriptors`（按 web_channels 枚举本机 bot）搅复杂，收益不抵成本。

**测试**：`test_topology_service` 的 set/assert 改 provider；`test_cluster_peer_e2e` 的 build_peer_descriptors 注入改 provider。全量 1064 passed（行为逐字节不变）。

## 2026-06-28 — workgroup 测试拆分进 tests/unit/workgroup/（yait #98 收尾）

代码隔离进 workgroup 包后，把测试也对称拆开。

**新建 `tests/unit/workgroup/`**（含 `__init__.py`，与 `tests/unit` 包结构一致）。

**拆 `test_workgroup.py`（598 行大杂烩，13 类混了 4+ 源模块）** 按源模块切分：
- `test_formatting.py` ← format_running_tasks / extract_specialist_response
- `test_heartbeat.py` ← is_silent_reply / _extract_action / _build_heartbeat_prompt / HeartbeatManager read-md / log facade / fork-skip（6 类 + 模块级 log 测试）
- `test_workspace_templates.py` ← seed_admin/specialist_workspace + 模板格式
- `test_manager.py` ← WorkgroupManager 纯方法
- `TestBackendForkCapability`（测 backend.supports_fork，**非 workgroup**）移到 `test_agent_backend_protocol.py`

**移入子目录**（去冗余 `workgroup_` 前缀）：integration / web_e2e / channel_adapter / prompt_fragment / config / template_loader / peer_service / agent_env。

**刻意留在 `tests/unit/`**（cluster/peer-routing 集成，非纯 workgroup 包测试）：`test_cluster_peer.py` / `test_cluster_peer_e2e.py` / `test_peer_conditional_wiring.py` / `test_topology_service.py`。

**不变量**：全量仍 1064 passed —— 纯重定位，零测试增减。用脚本按 class 边界切分 + 自动检测每文件实际用到的 import（避免 F401）。

## 2026-06-29 — 修 WebUI 跨机整条丢消息：/api/send fire-and-forget（yait #100）

症状：guest 浏览器正常聊天整条回复偶发消失 + 504 host timeout。非 buffer/queue（queue full 0 次）、非重连。

根因：`WebChannel.inject()` `await on_message(msg)` 阻塞整轮；guest 的 `/api/send` 经 `guest_client.call` 中继到 host，硬超时 30s（rpc.py:92）。任何 >30s 回复 → 504 → turn 丢。回复本应走独立 SSE `/api/stream`（`_proxy_via_host_stream`，无超时），send 不该等整轮。

修复：删掉 `inject` 里阻塞的 `await self.on_message(msg)`，换成一行 `asyncio.create_task(...)`。回复/错误走独立 SSE，`/api/send` 立即返回不撞 30s。活跃 turn task 一直在 await I/O、被 loop 引用，不会被 GC，无需额外跟踪。测试新增"慢 handler 下 inject 不阻塞"。

## 2026-06-30 — 前端 Web Component 试点：<tool-card>（无框架/无 build）

目标是**可维护性**（不是减行数）。把 tool-call 卡片抽成原生 custom element，放进独立文件 `static/components/tool-card.js`，作为前端组件化 + 文件拆分的第一刀。

- 新建 `<tool-card>`：自包含 DOM + `setCall()`/`setResult()` 生命周期；connectedCallback 延迟构建，兼容 history 的 detached fragment（setCall/setResult 先缓冲，连接时渲染）。
- app.js 删掉 `_buildToolCard`/`_applyToolResult`/`_argSummary` + `state.toolCards` 注册表，改为 `document.createElement("tool-card")` + 按 `data-tool-id` DOM 查找。
- index.html 加一行 `<script src="components/tool-card.js">`（classic script，全局注册，无 ES module/无 build）。

**净行数 +56**（组件 91 − app.js −35）—— 印证"WC 换结构不减行数"。收益是 tool-card 逻辑内聚、app.js 不再 juggle DOM 引用注册表。

**风险/限制**：前端 0 测试，本改动靠浏览器手验（live 工具卡 / 历史卡 / ✓✗ 结果 / 折叠 / subagent 嵌套）。若继续 WC 化，需先加 jsdom smoke-test 作安全网。

## 2026-06-30 — 前端组件测试：node --test + 自写 DOM stub（无 jsdom/npm）

WC 化后每个组件都要肉眼手验，迟早漏。加自动测试，但**不引入 jsdom/npm 工具链**（项目刻意 vanilla 无 build）。

方案：`static/test/dom-stub.js` 自写最小 DOM（createElement / classList / append / querySelector 支持 tag+[data-attr]+.class / dataset↔attribute / custom-element upgrade + connectedCallback），`load.js` 用 `vm.runInThisContext` 把 util.js + 组件源码 eval 进 stub 全局，`*.test.js` 用 Node 内置 `node:test` 断言。

接进主套件：`tests/unit/test_web_frontend.py` shell 出 `node --test static/test/*.test.js`，`uv run pytest` 一并跑（node 缺失则 skip）。

覆盖：util escapeHtml/renderMarkdown（含 XSS fallback）、tool-card upsert/幂等/result/synth/subagent/**detached-fragment 连接时渲染时序**、chat-message markdown/user 转义/setText 流式/data-id/时间戳。15 个子测试。

**为什么不 jsdom**：一个个人工具，自写 stub ~140 行覆盖组件实际用到的 DOM 子集，比拉进 npm + node_modules + package.json 这套异质工具链更轻、更符合无 build 的取向。stub 不是通用 DOM，够测这些组件即可。

## 2026-06-30 — 从 web 层移除 workgroup（前端 UI + server + set_main）

延续 route-B（workgroup 可插拔），把 workgroup 从 **web 层**整体拿掉。

**前端**：删 specialist 抽屉 UI（loadSpecialists/renderSpecialistsInto/selectSpecialist/isSpecialistChat + renderMachines specialist 块 + workgroup: platform 分支）；删 **"set as main"**（badge/链接/setMainSession/is_main）—— 它本就是 workgroup 专属（tooltip：heartbeat/peer 消息路由进该会话）。

**server.py**：删 `_handle_web_bots` 的 workgroup 分支、`/api/claude/resume` 的 workgroup backend/model/workspace/pool fallback、`set_workgroup_manager` + `workgroup_manager` 字段、`/api/sessions/set_main` 路由+handler、session 列表的 `is_main`。

**topology**：删 `local_bot_descriptors` 的 workgroup 分支（这才是 web 侧边栏 bot 列表的真实来源）。`build_peer_descriptors`（peer 消息路由）保留——属 workgroup 模块内部，非 web bot 列表。

**wiring**：删 `gateway._web_server.set_workgroup_manager`。

**保留**：`storage.{get,set,get_or_create}_main_chat_id`（workgroup peer/heartbeat/manager 仍用）。

**结果**：web 层零 workgroup（admin bot 不再在 UI 列出/可聊）。**workgroup 模块仍加载运行**（manager/heartbeat/peer），admin web channel 仍创建但不被列出 → 半死状态。clean finish = 删整个 workgroup 模块（另排）。app.js 1357→约 1230。全量 1066 passed。

## 2026-06-30 — 物理删除整个 workgroup 模块（route-B endgame）

接上一条（web 层移除 workgroup）的"另排 clean finish"，本次把 workgroup **整体物理删除**，结束 route-B（"workgroup 作为可插拔扩展隔离"）。

**删了什么**：
- 整个 `src/boxagent/workgroup/` 包（manager / heartbeat / peer_service / channel_adapter / config / formatting / http_routes / persistence / prompt_fragment / specialist_skills / task_queue / template_loader / templates / wiring / workspace_templates）。
- `tools/builtin/admin.py` + `tools/builtin/peer.py`（send_to_agent / send_to_peer / list_specialists 工具）。
- `AgentEnv.workgroup`（WorkgroupContext）+ `is_workgroup_admin` / `is_specialist` / `has_peer_channel` 属性 + `heartbeat_display_mode`。AgentEnv 现在零 workgroup 字段。
- `Router` 的 5 个 workgroup 字段（workgroup_agents / get_running_tasks / get_peers / has_peer_channel / workgroup_role）+ `dispatch_sync` 方法。Router 回归纯 auth/command/dispatch。
- gateway：PeerService 构造、`_peer` / `_workgroup_manager` 字段、`install_workgroup`、`/api/workgroup/*` + `/api/peer/send` 路由、InternalApiServer 的 `peer` / `workgroup_routes` 参数。
- config：`SpecialistConfig` / `WorkgroupConfig` 数据类、`AppConfig.workgroups` 字段、parse 循环。
- cluster/topology_service：`build_peer_descriptors` / `push_peers_snapshot_to_sats` / `set_local_workgroup_provider` / `_local_workgroup_names`（TopologyService 回归 machine-level only）。
- cluster/guest_client：`remote_peers` 字段 + `peers_snapshot` 帧处理。
- cluster/http_routes：`peer` 参数 + `/api/peer/send` + `/api/wg/peer/recv` 路由（只剩 `/api/guest/ws`）。
- sessions/storage：`get/set/get_or_create_main_chat_id` + `main_sessions.yaml` + 旧 `wg:`→`workgroup:` 迁移。
- log/categories：`HEARTBEAT_TICK/DRIVE/PAUSE`。
- transports/mcp/server：`/mcp/admin` + `/mcp/peer` 端点（只剩 base + telegram）。

**刻意保留**：
- backend `supports_fork` / `fork_and_send` 能力留在 Protocol（曾被 HeartbeatManager 唯一调用，现无调用者）—— 它是 backend 层通用能力，不属 workgroup 模块；删它要动三个 backend 实现 + SDK fork 调用链，属另一条重构，本次不碰，只把注释里对已删 HeartbeatManager 的引用去掉。
- `IncomingMessage.trusted`（cluster 绕 auth）、`ChannelCallback.webhook_name`（注释改为"bus replies"）。

**"peer" 语义收敛**：cluster 层的 "peer" 现在只指 **peer 机器**（拓扑里的 machine / `cluster.peer.up` 事件），不再有 "workgroup admin peer"。

**为什么物理删而不是继续留着**：route-B 隔离做完后，workgroup 模块虽可插拔但仍**加载运行**（半死状态：admin channel 创建但 UI 不列、heartbeat/peer 仍跑）。owner 决定一步到位删掉，避免长期养一个无 UI、无文档、无人用的子系统。隔离工作（PRs #15/#17）的价值正是让这次删除变成"删目录 + 摘调用点"而非大手术。

**测试**：workgroup 专属测试（`tests/unit/workgroup/`、`test_cluster_peer*`、`test_peer_conditional_wiring`、`test_prompt_tool_names`）随模块一并删除；共享测试里的 workgroup case 摘掉。基线 1066 → **886 passed**（降幅即被删的 workgroup/peer 测试）。`uv run boxagent --help` + 全量 import OK。
