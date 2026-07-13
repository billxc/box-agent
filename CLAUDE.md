# CLAUDE.md — BoxAgent AI 开发指南

> 给 AI agent 看的。你接手这个项目时，先读这个文件，再动手。

## 项目是什么

BoxAgent (BA) 是 **Personal Agent Network**：一个用户、多台机器、多个 AI agent，彼此协同，可从手机 / 浏览器 / iOS app / MCP 客户端访问。

单机单 agent 是最简部署形态，不是默认目标。完整产品形态是分布式 + 多 agent。BA 自己不做 agent 逻辑（tool calling / RAG / 记忆），全部交给 Claude CLI / Codex / SDK backend；BA 只负责编排、桥接、跨机互联。

详细愿景见 `docs/vision.md`，当前真实代码地图见 `docs/codebase-guide.md`。

## 项目阶段

**早期迭代中**。2026-03-20 从零开始（约 2 个月）。Telegram / Web / Cluster / EventBus 核心链路已落地并日常使用；愿景文档 (`docs/vision.md`) 仍有若干远景项未排期。**以代码和 `docs/codebase-guide.md` 为准，不要被 vision.md 带偏去实现没排期的功能。**

## 你要做事之前

1. 读 `docs/codebase-guide.md` — 当前实际架构
2. 读 `docs/decisions.md` — 为什么现在是这样

## OpenWiki

This repository has documentation located in the /openwiki directory.

Start here:
- [OpenWiki quickstart](openwiki/quickstart.md)

OpenWiki includes repository overview, architecture notes, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

When working in this repository, read the OpenWiki quickstart first, then follow its links to the relevant architecture, workflow, domain, operation, and testing notes.

---

## 迭代工作流（Step-by-Step）

不管是修 bug 还是加功能，都走这个流程。每一步做完确认了再往下走，不要跳步。

### Step 0: 明确任务

- Owner 给了什么任务？是 bug 还是 feature？
- **不确定范围就问，不要自己脑补**

### Step 1: 调研（最重要的一步）

**不要跳过这步直接写代码。**

- `grep -n` 找到所有相关代码路径
- 从入口开始追调用链，直到末端
- 读相关的测试文件，理解当前行为的预期
- 搞清楚：
  - 这个改动涉及哪些文件？
  - 调用链上下游是什么？
  - 有没有已知的相关 bug 或设计限制？
  - 改了之后会不会影响其他路径？

**输出**：向 owner 汇报调研结论，包括：
- 问题的根因（不是表象）
- 影响范围
- 涉及的文件和函数

### Step 2: 设计方案

- 提出具体方案（不是"我打算改一下"）
- 说清楚：改哪几个文件、加多少行、核心思路
- 如果有多个方案，列出 trade-off
- **等 owner 确认再动手**（除非 owner 说了"你看着办"）

### Step 3: 写测试（先红灯）

- 在对应的 `tests/unit/test_*.py` 里写测试
- 测试要覆盖：
  - 正常路径（happy path）
  - 边界情况（空输入、超长、超时）
  - 错误处理（异常、fallback）
  - 和相邻功能的交互
- 跑测试，**确认新测试是红灯**（fail）
- 确认旧测试全绿

```bash
# 只跑新测试，确认 fail
uv run pytest tests/unit/test_xxx.py::TestNewClass -x -q

# 确认旧测试没被破坏
uv run pytest tests/unit/test_xxx.py -x -q
```

#### 测试 backend / channel 用 MockBackend / MockChannel，**不要手搓 AsyncMock**

```python
from boxagent.testing.mocks import MockBackend, MockChannel

backend = MockBackend(session_id="sess_x", supports_session_persistence=True)
backend.script(["chunk1", "chunk2"])      # 脚本化 stream chunks
backend.script_handler(custom_async_fn)   # 复杂行为：raise / event 同步
backend.fail_next_turn("error msg")       # 模拟 turn 失败

channel = MockChannel()
# ... router 跑一轮 ...
assert backend.sends[-1].message == "..."          # 看 backend 收到啥
assert channel.sent_texts[-1] == ("chat_id", "...")
assert channel.streams[-1].chunks == ["chunk1", "chunk2"]
```

**黑盒 e2e 测试**：`tests/unit/test_router_e2e.py` 是范本——所有断言只看 MockBackend.sends + MockChannel.sent_texts/streams，从不 peek `router._compact_summaries` 之类的私有状态。新写整链路测试时照这个模板。

### Step 4: 实现

- 改动尽量小，能 30 行解决的不要写 100 行
- 不要顺手重构不相关的代码
- 不要加"以后可能用到"的抽象

### Step 5: 验证

```bash
# 跑全量测试
uv run pytest -x -q

# 确认测试数量没降
# 当前基线：977 collected（`uv run pytest --collect-only`，含 7 个 integration deselected）。workgroup 模块删除时曾降到 886，之后随功能与测试增长回升；此后只能涨不能降
```

- 新测试全绿
- 旧测试全绿
- 没有 warning 恶化

### Step 6: 更新文档

在**同一个 commit** 里更新相关文档：

| 改了什么 | 更新什么 |
|----------|----------|
| 改了行为 | `docs/codebase-guide.md` |
| 新的设计决策 | `docs/decisions.md` 追加条目 |

### Step 7: 提交 & 推送

```bash
git add -A
git diff --cached --stat  # 看一眼改了什么
git commit -m "fix(BUGxxx): 简短描述

详细说明（可选）"
git push
```

### Step 8: 汇报

向 owner 报告：
- 改了什么（文件 + 行数）
- 测试结果（X passed）
- commit hash

---

## 特殊场景的工作流变体

### 纯调研（owner 说"帮我看看"）

只做 Step 0-1，输出调研结论，不动代码。**调研要认真**：
- 不是 grep 一下就报告，要追完整条调用链
- 如果涉及多个路径（比如 Claude 和 Codex 两条线），都要看
- 结论要有具体的代码引用（文件名 + 行号 + 函数名）

### 快速修复（明显的小 bug）

Step 1 可以简化，但 Step 3（写测试）和 Step 5（验证）不能省。

### 大功能（跨多个文件）

- Step 2 要更详细，最好画出改动涉及的调用链
- 考虑拆成多个小 commit，每个 commit 独立可测
- 每个 commit 之间跑一次全量测试

---

## 架构概览（当前真实的）

```
   Telegram   Web UI / iOS app   MCP clients
       ↓            ↓                 ↓
   ┌──────────────────────────────────────┐
   │  Transports (channels)               │
   │  telegram / web / mcp                │
   └──────────────────┬───────────────────┘
                      ↓
   ┌──────────────────────────────────────┐
   │  Router (per-bot)                    │
   │  鉴权 / slash 命令 / dispatch        │
   └──────────────────┬───────────────────┘
                      ↓
   ┌──────────────────────────────────────┐
   │  AgentBackend (4 种)                 │
   │  claude-cli* / codex-cli /           │
   │  agent-sdk-claude / agent-sdk-copilot│
   │  (* claude-cli 已静默重定向到 SDK)   │
   └──────────────────────────────────────┘

   横切 / 装配:
   ┌─ Gateway ────── 装配根 (composition root)
   ├─ AgentManager ─ per-bot 生命周期 + watchdog
   ├─ Cluster ────── HostElection / TopologyService /
   │                 GuestRegistry / devtunnel
   ├─ Sessions ───── Storage + Pool + browser
   ├─ Scheduler ──── cron (isolate / append)
   └─ EventBus ───── log facade → SQLite EventStore →
                     web SSE / Telegram notifier / 跨机 sync
```

入口阅读顺序（详见 `docs/codebase-guide.md`）：
1. `gateway.py` — 装配根，`start()` 看启动顺序
2. `router/core.py` — `handle_message` → `_dispatch_one`
3. `agent/protocol.py` + `agent/backend_factory.py` — 4 个 backend 共通接口与分发
4. `agent/sdk_claude_process.py` — 主参考实现（`claude-cli` 也走它）
5. `transports/{telegram,web,mcp}/` — 三类 channel
6. `cluster/{host_election,registry,guest_client}.py` — 多机互联
7. `events/` 实现 + `log/` facade — 横切事件总线（业务代码只 import `boxagent.log`）

更详细的层级 + 三条信息流时序图见 `docs/current-architecture.md`。

## 开发规范

### Commit
- 小步提交，一个 commit 做一件事
- 前缀：`fix:`、`feat:`、`refactor:`、`docs:`、`tests:`
- Bug 修复写 `fix(BUGxxx):`

### 测试
- **改代码必须跑测试**：`uv run pytest -x -q`
- 新功能/bug 修复必须有测试
- 测试在 `tests/unit/`，集成测试在 `tests/integration/`（默认 skip）

### 文档
- **代码和文档同一个 commit**

### 命名（硬性规定，不许讨价还价）

**禁止使用缩写命名变量、参数、函数、属性。** 一律用完整英文单词。

❌ 历史血泪教训（已被批量清理过几轮的垃圾命名）：
`mid` / `sess` / `st` / `proc` / `mgr` / `cfg` / `resp` / `msgs` / `secs`
/ `caps` / `deco` / `opts` / `impl` / `inst` / `ch` / `cb` / `wg` / `gw`
/ `dest`（局部变量）/ `grep`（变量名）/ `sess_mid` / `local_mid` 等

✅ 必须写：
`message_id` / `machine_id` / `session` / `chat_state` / `process` /
`backend` / `manager` / `config` / `response` / `messages` / `seconds` /
`capabilities` / `decorator` / `options` / `implementation` /
`install_parser` / `channel` / `callback` / `workgroup` / `gateway` /
`dest_path` / `grep_pattern`

**例外（仅限以下，写新代码用之前先想清楚是不是真符合）**：
- 标准 Python 习语：`i` / `j` / `k` 循环计数、`e` exception、`f` 文件句柄、
  `args` / `kwargs` / `self` / `cls`、typing 的 `T` / `P` / `R` / `K` / `V`
- 第三方 API 关键字：argparse 的 `dest=`（API 强制）、`yolo`（CLI flag 名）
- 业内通用工具名：`pwsh`（Microsoft 官方简称）、`mcp` / `rpc` / `http` 等协议缩写
- 项目核心域词：`bot`（项目主语，非缩写）

**为什么这条卡这么死**：
1. 一份代码读 100 次写 1 次，缩写省的 3 个键打字代价远小于阅读时的歧义
2. `mid` 在 cluster/ 是 machine_id，在 transports/ 是 message_id —— 同一缩写两种含义，重命名时被坑过
3. `sess` 4 个字母刚好躲过简单的 ≤3 字符 audit，藏了几十处一次清完
4. `proc` 在本项目其实多数是 `AgentBackend` 不是 process，命名直接误导
5. `caps` 看着像帽子其实是 `capabilities`、`opts` 是 `options`、`inst` 是 `install_parser` —— 全是 self-documenting 不到位
6. 缩写无标准：`mgr` 还是 `manager`？`cfg` 还是 `config`？团队里每人写一种，grep 都 grep 不全

**写代码前自检**：
- 这个名字念出来像不像一个完整英文词？不像就展开
- 同事 / 三个月后的自己看到能不能 100% 猜对它代表什么？不能就展开
- 拿不准就用全名，没人会因为变量名长 8 个字母多加批评

**重命名工具**：`scripts/naming_audit.py`，跑一下看当前 suspect。

### Bug 追踪
- Bug 修复用 commit 前缀 `fix(BUGxxx):` 记录
- 已知问题记录在 `docs/decisions.md` 的相关条目中

## 当前状态速查

### 能用的
- ✅ Telegram 收发消息
- ✅ Claude CLI backend（流式、恢复、/compact）
- ✅ Codex CLI backend（codex exec --json，流式、恢复）
- ✅ 定时任务（append + isolate）
- ✅ 对话日志（JSONL per session）
- ✅ 流式消息自动分片（>4096 字符）
- ✅ 流式消息 Markdown 渲染（final edit）
- ✅ **Web UI channel**（默认开启，端口 9292，token 鉴权，移动端 UI，session 恢复，Claude 原生 session 选择恢复）
- ✅ **Session 链式保存**：跨 `/compact` 不丢历史
- ✅ **Hub-and-spoke 集群**：host 节点自动管理 devtunnel，guests WS 接入。**host 与 guest 的 web UI 都联邦显示全网 bot**（guest 端通过 `guest_client.remote_machines` 缓存 + 反向 RPC，对其他节点的 sessions/history/send/stream/schedules 等全部走 host 中继）
- ✅ **EventBus + EventStore**：SQLite 持久化的结构化事件日志，含 web UI（/events 页，category 树状导航）、跨机同步、retention sweeper、Telegram 推送通知器；通过 `boxagent.log` facade 写入（业务代码禁止直接 import `boxagent.events`）。
- ✅ **Web UI 主题系统**：shape × palette 两轴拆分（brutalist/phosphor/ink/soft/paper/neon/scandi × amber/matrix/synthwave/nord/gruvbox/mono/newsprint 等）。CSS 拆分为 `style.css` + `events.css` + `*.themes.css`。
- ✅ **Web UI 多页**：Chat / Events / Schedules / Logs 四页统一 top-left 导航，顶部 bar 统一高度；机器选择从 inline chips 改为 dropdown。
- ✅ **Schedules 页面**：跨机查看 schedule run 记录、内联 transcript、分页懒加载。
- ✅ **/compact 对齐 Claude CLI**：使用结构化 prompt；session 链式保存跨 compact 不丢历史（BUG88/89 已修）。
- ✅ **Cluster 容错**：host election 提升前 retry probe 防 split-brain；devtunnel 重复时通过 `devtunnel list` 解析并选 active 而非 raise。

### 有 Bug 的
- 已知问题见 `docs/decisions.md` 和 git history

### 没做的（已冻结）
- WebView2 桌面端
- Git 同步、知识库、多 worker pool
- PyPI 发布
- Specialist 跨机调度（workgroup 模块已整体删除；思路未理清，撤回未提交的初版）

## 做事原则

### 必须做的
- **先验证再报**：改完跑测试，确认通过再说"搞定"
- **先看代码再改**：不要凭猜测改，grep 确认调用链
- **改 bug 先写测试**：红灯 → 绿灯 → commit
- **改配置查文档**：不确定字段名先 grep 源码

### 绝对不做的
- **不要实现 vision.md 里没排期的功能**（WebView2 桌面端、Git 同步知识库等），除非 owner 明确要求。Web UI 已是默认 channel，不在此列。
- **不要重构没坏的东西**：稳定性优先；既有模块边界（router/agent/transports/cluster/events 等）是反复重构后的产物，不要"顺手"动
- **不要加抽象层**：能 30 行解决的不要写 class
- **不要删测试**：测试只能加不能减

### 踩过的坑（不要重蹈覆辙）
1. **Codex 事件用 `create_task` 导致乱序** — 已修为 `await`，不要改回去
2. **`_compact_summary` 在 send 前被消费** — 历史上 send 失败会丢上下文；已通过 BUG88/89 修复（storage 链式保存 + SDK walker raw-read）。结构上仍要小心：compact 流程触碰 send/save 顺序时验证链式保存不被破坏。
3. **Watchdog 重启后持有旧 backend 引用** — `_restart_bot` 没更新 watchdog
4. **Codex session 不能跨重启恢复** — 不要把 `session_id` 当成 Claude 式的恢复凭据
5. **stream 路径绕过了 `split_message`** — 已通过 `_split_stream` 自动翻页修复
6. **isolate scheduler 不继承 bot 完整配置** — workspace、model、MCP 都没传
7. **`/compact` 跨 compact 丢老 session（BUG88/89）** — 旧实现 send 前消费 summary、SDK walker substring 匹配 boundary。修法：storage 链式保存 + `_has_compact_boundary` 解析 JSON + raw-read jsonl。再动 compact 流程时跑 `tests/unit/test_session_chain*.py`。
8. **devtunnel 跨 region 同名 tunnel** — `devtunnel show` 是 region-ambiguous 的；同名 tunnel 会孤儿化、URL 漂移。必须用 `devtunnel list -j` + bare-name 过滤拿带 region 后缀的 `tunnelId`，>1 个时 warn + 选 active，不要自动 delete（删错 region 会把 host 自己关掉）。
9. **HostElection 提升前不 retry probe → split-brain** — 单次 probe 失败就当老 host 死了直接抢位会双 host。修法：promote 前 retry probe 一轮，且 probe 异常用 `repr(exc)` 记录类型，别只 str。
10. **Claude SDK monkey patch 用手写 wrapper 难维护** — 新 patch 一律走 dowhen `<return>` callback（`history/_sdk_patch.py`），SDK 升级时 fail-fast 比 getattr 兜底靠谱。
11. **`claude-cli` backend 已静默重定向到 `agent-sdk-claude`** — 旧 config 不报错；测试里别再 patch `ClaudeProcess`，patch `AgentSDKClaude`。`claude_process.py` 文件**已删除**（raw bot 占位 backend 改用 `_raw_backend_factory` 产 `AgentSDKClaude`）。
12. **业务代码不许直接 import `boxagent.events`** — 写事件用 `boxagent.log` facade（`get_logger(category)`）。`events/` 是实现细节，未来要换 backend 不破坏调用方。
13. **guest 重连竞态：旧 `handle_ws` 协程 finally 别碰共享状态** — `registry.py` 的 finally 里 `detach_link` 必须和 `sessions.pop` 同守卫（`not session._closed`）。被顶掉的旧协程若无条件 detach，会把新连接刚 attach 的 ClusterBus link 删掉，留下"`sessions` 里在线、`cluster_bus._links` 里不可达"的幽灵态，令该 guest 所有跨机 RPC 应答被丢弃全超时。诊断口诀：拓扑显示 online 但 `host→该机` 秒回 `unreachable` = session 在、link 没了。**naive 重启会再触发同一竞态**，清幽灵态要"停→等 host 标 offline→再起"或重启 host。动 registry finally 跑 `tests/unit/test_cluster_reconnect_race.py`。

## 文件地图

| 路径 | 干什么的 |
|------|----------|
| `src/boxagent/gateway.py` | Gateway 装配 + InternalApiServer + 内部 HTTP API |
| `src/boxagent/router/core.py` | Router：消息分发、命令、typing、transcript |
| `src/boxagent/router/callback.py` | ChannelCallback、TextCollector、log_turn |
| `src/boxagent/router/commands/` | slash 命令（@command 装饰器自动发现） |
| `src/boxagent/router/context.py` | 首条消息的 system prompt context 拼接 |
| `src/boxagent/agent/protocol.py` | AgentBackend Protocol + BACKEND_KINDS |
| `src/boxagent/agent/backend_factory.py` | create_backend() 按 ai_backend 分发 |
| `src/boxagent/agent/agent_manager.py` | AgentManager：per-bot 生命周期、watchdog |
| `src/boxagent/agent/base_cli.py` | CLI backend 共享基类 |
| `src/boxagent/agent/codex_process.py` | Codex CLI backend |
| `src/boxagent/agent/sdk_claude_process.py` | claude_agent_sdk in-process backend（唯一 Claude backend，`claude-cli` 也走它） |
| `src/boxagent/agent/sdk_copilot_process.py` | GitHub Copilot SDK in-process backend |
| `src/boxagent/agent/mcp_endpoints.py` | pick_mcp_endpoints() 共享 helper |
| `src/boxagent/agent/session_info.py` | SessionInfo dataclass — 容量 / recap / cwd / git_branch 等会话级元数据 |
| `src/boxagent/transports/telegram/channel.py` | Telegram 输入输出、流式编辑 |
| `src/boxagent/transports/web/channel.py` | Web UI channel（per-chat SSE 队列） |
| `src/boxagent/transports/web/server.py` | Web UI HTTP server |
| `src/boxagent/transports/web/log_file.py` | `read_tail()` — JSON-line 反向分页 + level/grep 过滤（Logs 页用） |
| `src/boxagent/transports/web/static/` | Web UI 前端（vanilla HTML/CSS/JS，markdown 流式渲染；含 chat/events/schedules 三页 + shape×palette 主题） |
| `src/boxagent/transports/mcp/server.py` | MCP HTTP server (create_mcp_app + McpHttpServer) |
| `src/boxagent/transports/telegram/splitter.py` | 长消息拆分 |
| `src/boxagent/transports/telegram/md_format.py` | Markdown 格式转换（Telegram MarkdownV2） |
| `src/boxagent/cluster/registry.py` | Host：guest WS 接入 + GuestRegistry + wire protocol |
| `src/boxagent/cluster/guest_client.py` | Guest：dial host + auto-reconnect + RPC 转发 |
| `src/boxagent/cluster/host_election.py` | host vs guest 选举 + failover |
| `src/boxagent/cluster/topology_service.py` | machine 描述符 |
| `src/boxagent/cluster/tunnel.py` | host 自动管理 devtunnel 进程 |
| `src/boxagent/sessions/storage.py` | Session 持久化（sessions.yaml 绑定 + session_history.yaml recents + transcripts） |
| `src/boxagent/sessions/info_builder.py` | build_session_info() — 聚合 history + storage 装配 `SessionInfo`（`/api/session_info` 用） |
| `src/boxagent/sessions/browser/` | /sessions + /resume 浏览器（合并 history + Storage） |
| `src/boxagent/history/` | Read-only adapters：Claude/Codex/Copilot 原生 transcript |
| `src/boxagent/tools/registry.py` | @boxagent_tool 装饰器 + tools_for() |
| `src/boxagent/tools/builtin/` | 内置 MCP 工具（schedule/sessions/telegram_media/log_event） |
| `src/boxagent/tools/adapters/` | backend-specific MCP 包装（mcp_http / claude_sdk / copilot_sdk） |
| `src/boxagent/scheduler/engine.py` | Scheduler 主循环（task start/done/fail 含 duration 与 output） |
| `src/boxagent/scheduler/cli.py` | `boxagent schedule` 子命令 |
| `src/boxagent/scheduler/http_routes.py` | SchedulerHttpRoutes — `POST /api/schedule/run` 等触发端点 |
| `src/boxagent/log/` | Public log facade（Category 常量 + NullLogger） — 业务代码写事件入口 |
| `src/boxagent/events/` | EventBus + SQLite EventStore + 跨机 sync + retention sweeper + Telegram notifier + web SSE stream（业务代码禁止直接 import） |
| `src/boxagent/web_error_middleware.py` | aiohttp middleware：把 handler 异常打入 event log |
| `ios/BoxAgent/` | iOS Swift app — 消费 Web `/api/sse` + `/api/history` + `/api/sessions`，等价于移动 Web client（不是独立 transport） |
| `src/boxagent/testing/mocks.py` | **MockBackend / MockChannel — 写测试时用这个，别手搓 AsyncMock** |
| `src/boxagent/config.py` | AppConfig / BotConfig |
| `src/boxagent/agent_env.py` | AgentEnv / ChannelInfo（每条消息的 env 快照） |
| `src/boxagent/watchdog.py` | 自动重启 |
| `src/boxagent/doctor.py` | doctor --fix 依赖检查 |
| `docs/codebase-guide.md` | **当前架构的真实描述** |
| `docs/current-architecture.md` | 4 层结构 + 3 条信息流时序图 + 数据类污染分析 |
| `docs/vision.md` | 远景（参考，不要当指令） |
| `docs/decisions.md` | 决策记录 |
| `docs/archive/` | 已实现 / 未采纳的旧设计提案 |

## 快速命令

```bash
# 跑测试
uv run pytest -x -q

# 跑单个文件
uv run pytest tests/unit/test_telegram_channel.py -x -q

# 启动
uv run boxagent --config ~/.boxagent/config.yaml

# 看 git 状态
git log --oneline -5 && git status -sb
```

## 需求 / Issue 跟踪：yait

本仓库用 [`yait`](https://) 管理需求和调研记录，项目名固定为 **`box-agent`**（命名项目，存在 `~/.yait/projects/box-agent/`，不污染仓库）。

```bash
# 列出所有 issue
yait -P box-agent list

# 查看某个 issue 详情
yait -P box-agent show <ID>

# 创建（注意 type ∈ {feature,bug,enhancement,misc}, priority ∈ {p0..p3,none}）
yait -P box-agent new "标题" -t feature -p p2 -l workgroup --body "正文"

# 关联 / 评论 / 关闭
yait -P box-agent link <ID> {blocks|depends-on|relates-to} <ID>
yait -P box-agent comment <ID> "进展"
yait -P box-agent close <ID>
```

### 约定

- **每次接到需求或做完调研都先开 issue**（甚至是只为留痕的调研也开），把背景 / 范围 / 参考文件路径 + 行号 / 开放问题写进 body
- 大需求拆成主 issue + 子 issue，用 `blocks` / `depends-on` 串起来
- 标签建议：`workgroup` / `cluster` / `config` / `docs` / `refactor` 等按模块走
- 调研类 issue 在标题前加 `调研：`

### 代码注释
- 代码注释写太长，对读代码的人是一种负担，所以要言简意赅
- 注释用中文写
