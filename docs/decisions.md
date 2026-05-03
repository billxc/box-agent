# 决策日志

记录每次偏离原始设计的决定，以及原因。

---

## 2026-03-22: 删除 max_workers 和 display.streaming

**决定**: 从 BotConfig 中删除 `max_workers` 和 `display_streaming` 字段。

**原因**: BoxAgent 定位是轻量桥接，不做并发管理（backend 自己处理），所以 worker pool 不会实现。`display.streaming` 虽然能解析但运行时没有消费，Telegram 始终流式输出。与其留着死代码误导人，不如删掉，需要时再加。

---

## 2026-03-22: 文档归档

**决定**: 将所有早期设计文档移入 `docs/archive/`，只保留反映当前实现的文档。

**原因**: 项目从设计到实现过程中大幅收敛，早期文档（V1 设计、V2 路线图、实现计划）与实际代码不一致，容易误导维护者。`codebase-guide.md` 已经准确描述了当前状态。

**归档的文件**:
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

**决定**: 一台机器为 host（自动 `devtunnel create + host`），其余作为 satellite WS 主动 dial 进来；host web UI 联邦显示所有节点的 bot，用户选远端 bot 时由 host 通过 RPC over WS 转发到对应 satellite。

**原因**: 替代 Discord 作为机器间互通底层；要求"一个浏览器管所有机器"。Hub-and-spoke 比 peer-to-peer 简单：只一个公开端点，satellite 在 NAT/防火墙后不需要暴露端口。

**关键设计**:
- 配置只在共享 `config.yaml` 顶层 `cluster: {host, tunnel_name, token}`，每台机器靠自己 `node_id` 自动决定角色，**不需要任何 local 配置或手动回填**。
- Devtunnel 创建**不带 `-a`**，默认认证 → 同 Microsoft 账号才能 mint connect token。Satellite 启动时 `devtunnel token --scopes connect` 现场拿 JWT，放 `X-Tunnel-Authorization` header。
- 三道安全门：devtunnel JWT（账号级）+ `cluster.token`（hello frame）+ `web_token`（HTTP 层）。
- WS RPC 协议是通用 envelope（`rpc` / `rpc_resp` / `rpc_stream` / `rpc_end`），host 几乎不用维护 per-endpoint 转发逻辑；SSE 经 sat 拆 `data:` 行 → host 拼回。

**未做**：specialist 跨机调度（思路未理清，已撤回未提交的初版）。

---

## 2026-05-02: 配置文件如何分共享 vs 本地

**决定**: 跨机器共享的配置（cluster 拓扑、bots 定义、workgroup 定义）放共享 `config.yaml`；机器自身身份（`node_id`）放 `local.yaml`。机器角色（host/satellite）由共享配置 `cluster.host` 与本地 `node_id` 比对自动得出，避免角色信息散落到本地。

**原因**: 早期把 `satellite_token` / `host_url` 都放共享 config 会让每台机器都觉得自己是 host；放本地 `local.yaml` 又破坏了"共享配置即真相"的原则。最终方案：拓扑共享，身份本地，角色派生。

## 2026-05-03: Cluster RPC 入站路由必须挂在 web UI app（不是内部 API app）

**决定**: 凡是 `sat_client` 通过 WS RPC 转发回本机 HTTP 的端点（当前是 `/api/wg/peer/recv`，未来类似端点同理），都必须注册到 `_start_web_http()` 创建的 `wapp` 上，而**不是** `_start_http()` 创建的 `app`。

**原因**: gateway 跑两个独立 aiohttp Application：`app` 在内部 API 端口（动态分配），`wapp` 在 web UI 端口（默认 9292）。`sat_client` 用 `local_web_port` 转发 RPC（gateway.py:282 传的是 `web_port`），所以入站只命中 `wapp`。把路由放 `app` 会让每条跨机 peer 消息**沉默 404**。

**配套**: `Gateway.send_peer` 必须检查 `SatelliteSession.call(...)` 返回的 `status` 字段（404/500 都是合法 round-trip，不抛异常）。仅 transport 成功不等于业务成功，应把非 2xx 当失败上抛，避免 admin AI 收到 "Message sent" 但对端从未收到。

**回归测试**: `tests/unit/test_cluster_peer_e2e.py::test_peer_recv_route_registered_on_web_app_not_api_app` + `::test_send_peer_surfaces_404_from_sat_recv`。


## 2026-05-03: Workgroup peer discovery comes from cluster registry, not peers.yaml

**决定**: 删掉 `~/.boxagent/peers.yaml` 的读取路径。Workgroup admin 在 system context 里看到的 peer 列表，由 `Gateway._build_peer_descriptors(exclude=self_name)` 动态生成，源 = `_workgroup_mgr.routers`（本机 workgroup-kind） + `_sat_registry.list_bots()`（远端 workgroup-kind） + `_sat_registry.history`（离线带 history）。

**原因**: yaml 是手维护静态副本，跟动态 cluster 状态分叉：
- 加/删 sat 要跨机同步 yaml；不同步就有 admin "看不见" peer 或 "看到不存在" 的 peer
- 没有在线/离线状态
- 同时维护两套真相，cluster registry 里早已知道一切

新链路：`Router.get_peers` callable → `AgentEnv.peers` tuple → `build_session_context` 渲染。WorkgroupManager 通过 `_peer_provider` 钩子拿 Gateway 的 helper。

**已知不足**: satellite 节点的 `_sat_registry` 是 None — 只能看到本机 workgroup，看不到 host 上的或其他 sat 上的 workgroup。需要 sat→host 反向 RPC 查询补齐（yait #67）。
