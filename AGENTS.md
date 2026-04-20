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
Telegram → TelegramChannel → Router → ClaudeProcess / CodexProcess / ACPProcess
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

### 有 Bug 的
- 已知问题见 `docs/decisions.md` 和 git history

### 没做的（已冻结）
- Web UI channel
- WebView2 桌面端
- Git 同步、知识库、多 worker pool
- PyPI 发布

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
| `src/boxagent/gateway.py` | 组件装配、启停、HTTP API |
| `src/boxagent/router.py` | 消息分发、命令、typing、transcript |
| `src/boxagent/router_callback.py` | ChannelCallback 实现、TextCollector、日志 |
| `src/boxagent/router_commands.py` | 系统命令处理器（/status, /new, /cancel 等） |
| `src/boxagent/context.py` | 用户/chat 上下文数据类 |
| `src/boxagent/agent/base_cli.py` | CLI backend 共享基类 |
| `src/boxagent/agent/claude_process.py` | Claude CLI backend |
| `src/boxagent/agent/codex_process.py` | Codex CLI backend |
| `src/boxagent/agent/acp_process.py` | Codex ACP backend |
| `src/boxagent/channels/telegram.py` | Telegram 输入输出、流式编辑 |
| `src/boxagent/channels/splitter.py` | 长消息拆分 |
| `src/boxagent/channels/mdv2.py` | Markdown → Telegram MarkdownV2 转换 |
| `src/boxagent/scheduler.py` | 定时任务 |
| `src/boxagent/schedule_cli.py` | schedule 子命令（add/list/show 等） |
| `src/boxagent/config.py` | 配置解析 |
| `src/boxagent/storage.py` | Session 持久化 |
| `src/boxagent/watchdog.py` | 自动重启 |
| `src/boxagent/paths.py` | 路径解析 |
| `src/boxagent/doctor.py` | doctor --fix 依赖检查 |
| `src/boxagent/mcp_server.py` | Telegram 媒体 MCP 工具 |
| `docs/codebase-guide.md` | **当前架构的真实描述** |
| `docs/vision.md` | 远景（参考，不要当指令） |
| `docs/decisions.md` | 决策记录 |

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
