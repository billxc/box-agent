# AGENTS.md — BoxAgent AI 开发指南

> 给 AI agent 看的。你接手这个项目时，先读这个文件，再动手。

## 项目是什么

BoxAgent (BA) 是 Telegram → AI agent 的桥接网关。用户在手机上发消息，BA 把它转给 Claude CLI 或 Codex，流式回复推回 Telegram。单人自用，不做多用户。

## 项目阶段

**早期迭代中**。2026-03-20 从零开始，核心链路已跑通并日常使用。愿景文档 (`docs/vision.md`) 定了很多远景，但实际落地大幅收敛。**以代码和 `docs/codebase-guide.md` 为准，不要被 vision.md 带偏去实现没排期的功能。**

## 你要做事之前

1. 读 `docs/codebase-guide.md` — 当前实际架构
2. 读 `docs/decisions.md` — 为什么现在是这样

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
# 当前基线：444 passed（随开发递增，只能涨不能降）
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
Telegram → TelegramChannel → Router → ClaudeProcess / CodexProcess / SDKClaude / SDKCopilot
                                  ↓
                              Gateway（装配所有组件）
                              Storage（session 持久化）
                              Scheduler（定时任务）
                              Watchdog（自动重启）
```

核心文件 6 个：`gateway.py`、`router.py`、`claude_process.py`、`codex_process.py`、`telegram.py`、`scheduler.py`。

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
- ✅ **Hub-and-spoke 集群**：host 节点自动管理 devtunnel，satellites WS 接入，host web UI 联邦显示所有节点的 bot
- ✅ **Workgroup over web**：admin/specialist 内部消息走 WebChannel。跨 admin peer messaging 走 cluster RPC（同机 in-process / 跨机 `POST /api/wg/peer/recv`）。

### 有 Bug 的
- 已知问题见 `docs/decisions.md` 和 git history

### 没做的（已冻结）
- WebView2 桌面端
- Git 同步、知识库、多 worker pool
- PyPI 发布
- Specialist 跨机调度（思路未理清，已撤回未提交的初版）

## 做事原则

### 必须做的
- **先验证再报**：改完跑测试，确认通过再说"搞定"
- **先看代码再改**：不要凭猜测改，grep 确认调用链
- **改 bug 先写测试**：红灯 → 绿灯 → commit
- **改配置查文档**：不确定字段名先 grep 源码

### 绝对不做的
- **不要实现 vision.md 里没排期的功能**（Web UI、WebView2 等），除非 owner 明确要求
- **不要重构没坏的东西**：项目 3 天大，稳定性优先
- **不要加抽象层**：能 30 行解决的不要写 class
- **不要删测试**：测试只能加不能减

### 踩过的坑（不要重蹈覆辙）
1. **Codex 事件用 `create_task` 导致乱序** — 已修为 `await`，不要改回去
2. **`_compact_summary` 在 send 前被消费** — send 失败会丢上下文，这是已知 bug
3. **Watchdog 重启后持有旧 backend 引用** — `_restart_bot` 没更新 watchdog
4. **Codex session 不能跨重启恢复** — 不要把 `session_id` 当成 Claude 式的恢复凭据
5. **stream 路径绕过了 `split_message`** — 已通过 `_split_stream` 自动翻页修复
6. **isolate scheduler 不继承 bot 完整配置** — workspace、model、MCP 都没传

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
| `src/boxagent/agent/claude_process.py` | Claude CLI backend |
| `src/boxagent/agent/codex_process.py` | Codex CLI backend |
| `src/boxagent/agent/sdk_claude_process.py` | claude_agent_sdk in-process backend |
| `src/boxagent/agent/sdk_copilot_process.py` | GitHub Copilot SDK in-process backend |
| `src/boxagent/agent/mcp_endpoints.py` | pick_mcp_endpoints() 共享 helper |
| `src/boxagent/transports/telegram/channel.py` | Telegram 输入输出、流式编辑 |
| `src/boxagent/transports/web/channel.py` | Web UI channel（per-chat SSE 队列） |
| `src/boxagent/transports/web/server.py` | Web UI HTTP server |
| `src/boxagent/transports/web/static/` | Web UI 前端（vanilla HTML/CSS/JS，markdown 流式渲染） |
| `src/boxagent/transports/mcp/server.py` | MCP HTTP server (create_mcp_app + McpHttpServer) |
| `src/boxagent/transports/telegram/splitter.py` | 长消息拆分 |
| `src/boxagent/transports/telegram/md_format.py` | Markdown 格式转换（Telegram MarkdownV2） |
| `src/boxagent/cluster/registry.py` | Host：guest WS 接入 + GuestRegistry + wire protocol |
| `src/boxagent/cluster/guest_client.py` | Guest：dial host + auto-reconnect + RPC 转发 |
| `src/boxagent/cluster/host_election.py` | host vs guest 选举 + failover |
| `src/boxagent/cluster/peer_service.py` | send_to_peer 跨机投递 |
| `src/boxagent/cluster/topology_service.py` | peer/machine 描述符 |
| `src/boxagent/cluster/tunnel.py` | host 自动管理 devtunnel 进程 |
| `src/boxagent/sessions/storage.py` | Session 持久化（session_history.yaml + transcripts） |
| `src/boxagent/sessions/browser/` | /sessions + /resume 浏览器（合并 history + Storage） |
| `src/boxagent/history/` | Read-only adapters：Claude/Codex/Copilot 原生 transcript |
| `src/boxagent/tools/registry.py` | @boxagent_tool 装饰器 + tools_for() |
| `src/boxagent/tools/builtin/` | 内置 MCP 工具（admin/peer/schedule/sessions/telegram_media） |
| `src/boxagent/tools/adapters/` | backend-specific MCP 包装（mcp_http / claude_sdk / copilot_sdk） |
| `src/boxagent/scheduler/engine.py` | Scheduler 主循环 |
| `src/boxagent/scheduler/cli.py` | `boxagent schedule` 子命令 |
| `src/boxagent/workgroup/manager.py` | WorkgroupManager：admin + specialist 编排 |
| `src/boxagent/workgroup/heartbeat.py` | HeartbeatManager：admin 周期性自驱动 |
| `src/boxagent/testing/mocks.py` | **MockBackend / MockChannel — 写测试时用这个，别手搓 AsyncMock** |
| `src/boxagent/config.py` | AppConfig / BotConfig / WorkgroupConfig / SpecialistConfig |
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
