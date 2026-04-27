# Workgroup 设计文档

## 概述

Workgroup 是 BoxAgent 的多 agent 协作系统。一个 workgroup 包含一个 **admin**（管理者）和若干 **specialist**（执行者）。Admin 接收用户指令，通过 MCP tool 将任务委派给 specialist，specialist 处理后返回结果，admin 汇总回复用户。

Workgroup 是独立的配置单元，不依赖 `bots` 配置节。

## 架构

```
用户 ──── Discord/Telegram ──── Admin Agent
                                    │
                           send_to_agent MCP tool
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              Specialist-1    Specialist-2    (动态创建...)
              (内部 agent)    (内部 agent)

          Heartbeat ──── 定时检查 ──── fork session 判断
                                          │
                                      有事 → dispatch 到 admin 主 session
                                      无事 → NO_REPLY
```

- **Admin**: 有自己的 Discord category + admin channel，用户在 category 下的频道与 admin 对话
- **Specialist**: 独立 Claude Code agent，有自己的 workspace 和 session，只通过 `send_to_agent` 可达
- **Heartbeat**: 定时 fork session 做只读判断，有事才 dispatch 到 admin 主 session
- **通信**: Admin 调用 MCP tool → Gateway HTTP API → WorkgroupManager → specialist 的 `dispatch_sync` → 返回结果

## 配置

```yaml
workgroups:
  war-room:
    workspace: war-room                   # 相对于 ~/.boxagent/
    enabled_on_nodes: mbp                 # 只在指定节点启动（空 = 所有节点）
    discord_bot_id: mbp                   # Discord bot 身份（引用 discord_bots.yaml）
    allowed_users: [942620361332260936]
    ai_backend: claude-cli
    model: ""
    yolo: true
    display_name: War Room Admin
    display:
      tool_calls: silent
    extra_skill_dirs:
      - ./xc-notes/openclaw/skills

    # Heartbeat 配置
    heartbeat_interval_seconds: 300       # 0 = 禁用
    display_heartbeat: true               # 在 Discord 显示 heartbeat 信息

    # Admin 频道配置
    admin:
      discord_category: 1497645743123464333   # admin 监听的 Discord category
      discord_admin_channel: 1497645761830322267  # heartbeat/通知用的文字频道

    # Specialist 列表（静态）
    specialists:
      dev-1:
        display_name: Developer 1
        discord_channel: 1497835185402544249   # 可选，有则显示对话
        # model: sonnet                       # 可选，默认继承 workgroup
        # ai_backend: claude-cli              # 可选，默认继承 workgroup
```

### 字段继承关系

| Specialist 字段 | 默认值 |
|----------------|--------|
| `model` | workgroup.model |
| `workspace` | `{workspace}/.boxagent-workgroup/specialists/{name}/` |
| `ai_backend` | workgroup.ai_backend |
| `extra_skill_dirs` | workgroup.extra_skill_dirs |
| `yolo` | workgroup.yolo |

### Node 过滤

workgroup 和 bot 一样支持 `enabled_on_nodes`：

```yaml
enabled_on_nodes: mbp           # 单节点
enabled_on_nodes: [mbp, nas]    # 多节点
```

空值或不设置 = 所有节点都启动。config 校验也会跳过不在当前节点的 workgroup（避免 Discord category 冲突误报）。

## 目录结构

### 代码

```
src/boxagent/workgroup/
├── __init__.py              # re-exports WorkgroupManager, HeartbeatManager
├── manager.py               # WorkgroupManager — 生命周期、委派、动态创建
├── heartbeat.py             # HeartbeatManager — 定时检查 + fork session
├── mcp_admin.py             # boxagent-admin MCP — admin 专用工具
├── workspace_templates.py   # 模板加载 + workspace seed（两层 prompt）
└── templates/               # .md 模板文件
    ├── admin/               # admin 的 CLAUDE.md, SKILL.md, templates.md, HEARTBEAT.md
    └── specialist/          # specialist 的 CLAUDE.md, SKILL.md, templates.md
```

### Workspace

```
~/.boxagent/war-room/.boxagent-workgroup/
├── admin/                          # Admin workspace
│   ├── .claude/
│   │   ├── CLAUDE.md               # 系统层：每次启动覆盖
│   │   └── skills/superboss/
│   │       ├── SKILL.md            # 系统层：每次启动覆盖
│   │       └── references/templates.md
│   ├── BOXAGENT.md                 # 用户层：用户自由编辑，不被覆盖
│   ├── HEARTBEAT.md                # 用户层：heartbeat checklist
│   └── heartbeat.log              # heartbeat 执行日志（自动生成）
├── specialists/
│   ├── dev-1/                      # Specialist workspace
│   │   ├── .claude/
│   │   │   ├── CLAUDE.md           # 系统层
│   │   │   └── skills/supercrew/
│   │   │       ├── SKILL.md        # 系统层
│   │   │       └── references/templates.md
│   │   └── .git/
│   └── dev-2/
│       └── ...
└── worktrees/                      # git worktree 共享目录
```

## 两层 Prompt 系统

| 层级 | 文件 | 行为 | 用途 |
|------|------|------|------|
| **系统层** | `.claude/CLAUDE.md`, `SKILL.md`, `templates.md` | 每次 gateway 启动覆盖 | 角色定义、MCP 工具、工作流规范 |
| **用户层** | `HEARTBEAT.md`, `BOXAGENT.md` | 只创建一次，不覆盖 | 项目指令、heartbeat checklist |

- 系统层更新后无需手动删除文件，下次启动自动生效
- 用户层的 `BOXAGENT.md` 通过 `context.py` 注入到 `--append-system-prompt`
- 如果内容没变，系统层文件不会重写（避免不必要的磁盘 I/O）

## MCP 工具

| Tool | 用途 |
|------|------|
| `list_specialists()` | 列出所有专家及详情（name, model, workspace, builtin/dynamic, running tasks） |
| `send_to_agent(agent_name, message)` | 异步派发任务给专家 |
| `create_specialist(name, model?)` | 动态创建新专家（自动分配 workspace + Discord channel） |
| `delete_specialist(agent_name)` | 删除动态专家（停进程 + 删 Discord channel + 清持久化） |
| `reset_specialist(agent_name)` | 清除专家 session，下次任务从零开始 |

### MCP Server 分配

| MCP Server | 文件 | 注入条件 |
|------------|------|---------|
| `boxagent` | mcp_server.py | 所有 agent |
| `boxagent-admin` | workgroup/mcp_admin.py | `is_workgroup_admin=True` |
| `boxagent-telegram` | mcp_telegram.py | 有 Telegram token |

## XML 标签协议

### Specialist 回复

`send_to_agent` 自动在 prompt 末尾追加指令，要求专家用 `<specialist_response>` 标签包裹最终回复。系统从标签中提取干净内容，去掉思考过程。

```
admin 发送: "实现 auth 中间件"
↓ 系统追加 XML 指令
specialist 回复: "Let me think... <specialist_response>Done. Created auth.py with tests.</specialist_response>"
↓ 系统提取
admin 收到: "Done. Created auth.py with tests."
```

无标签时 fallback 到原始文本。

### Heartbeat 决策

heartbeat prompt 要求用 `<heartbeat_action>` 标签：

```xml
<!-- 无事 -->
<heartbeat_action>NO_REPLY</heartbeat_action>

<!-- 有事 -->
<heartbeat_action>
Check dev-mac status, task running 45+ minutes
</heartbeat_action>
```

## Heartbeat

### 工作原理

1. gateway 启动后，等 Discord ready，立即执行第一次 tick
2. 之后每隔 `heartbeat_interval_seconds` 执行一次
3. 每次 tick：
   - 读取 `HEARTBEAT.md`
   - **Fork session**（不污染 admin 主会话）做只读判断
   - 判断结果是 `NO_REPLY` → 跳过
   - 有事 → dispatch 到 admin 主 session 执行

### Session Fork

- heartbeat 通过 `pool._get_ctx(chat_id)` 触发 lazy load，从 storage 恢复 session_id
- 用 `claude --resume <session_id> --fork-session` 创建独立分支
- fork session 能看到 admin 的对话历史但不会污染它
- 没有可 fork 的 session 时启动全新 session（仍能读 CLAUDE.md）

### Heartbeat Prompt 包含的信息

- 当前时间
- gateway uptime
- 正在运行的专家任务（task_id、target、已运行时间、active/queued 状态）
- `HEARTBEAT.md` 内容

### 显示

- `display_heartbeat: true` 时在 `admin_discord_channel` 显示 heartbeat 信息
- Guild text channel → 用 "ba-heartbeat" webhook（不会被 admin 收到）
- DM channel → fallback 到 `send_text`，加 `───── heartbeat ─────` 分隔符

### 日志

每次 tick 写入 `{admin_workspace}/heartbeat.log`：

```
=== 2026-04-27 10:30:00 ===
source_session: e084fa8a-0410-4544-abbd-b7951b39973e
fork_session:   abc12345-...
silent: true

--- prompt ---
[HEARTBEAT CHECK]
...

--- raw response ---
<heartbeat_action>NO_REPLY</heartbeat_action>

--- extracted action ---
NO_REPLY
```

## Session Context 注入

Admin 每次 turn 的 system prompt 包含实时工作状态：

```
[Workgroup]
You are the admin of a workgroup. Available specialist agents:
- dev-1
- dev-test
- pm-qa

Currently running specialist tasks:
  - dev-1-3: dev-1 (running 12m 30s) [active]
  - pm-qa-1: pm-qa (running 2m 5s) [queued]

Use the send_to_agent MCP tool to delegate tasks to specialists.
[/Workgroup]
```

`[active]` = 进程正在跑 Claude CLI turn，`[queued]` = 任务已 dispatch 但进程还没执行。

## 消息流

### 1. 用户 → Admin → Specialist → Admin → 用户

```
1. 用户在 Discord 频道发送消息
2. Discord on_message → admin router.handle_message()
3. Admin AI 处理，决定委派
4. Admin 调用 send_to_agent("dev-1", "实现功能...") MCP tool
5. MCP tool → HTTP POST /api/workgroup/send → WorkgroupManager.send_to_specialist()
6. WorkgroupManager:
   a. 在 specialist 的 Discord 频道发布任务（webhook，人类可见）
   b. prompt 末尾追加 <specialist_response> XML 指令
   c. 调用 specialist router.dispatch_sync()（streaming 到 specialist 频道）
   d. 从 <specialist_response> 标签提取干净结果
7. 结果通知到 admin Discord 频道（含内容预览）
8. 完整结果作为 IncomingMessage 发给 admin router
9. Admin AI 汇总，回复用户
```

### 2. 动态创建 Specialist

```
1. Admin 调用 create_specialist("code-reviewer") MCP tool
2. MCP tool → HTTP POST /api/workgroup/create_specialist
3. WorkgroupManager:
   a. 在 Discord category 下创建 #code-reviewer 频道
   b. 创建 workspace 目录 + .git（_ensure_git_repo）
   c. 写入系统层模板文件（CLAUDE.md, SKILL.md）
   d. 创建 backend process + session pool + router
   e. 保存到 local/workgroup_specialists.yaml（重启后恢复）
   f. 更新 admin 的 workgroup_agents 列表
4. Specialist 立即可用
```

### 3. 删除 Specialist

```
1. Admin 调用 delete_specialist("code-reviewer") MCP tool
2. Built-in specialist（config.yaml 定义的）不能删除
3. 动态 specialist：停进程 → 停 pool → 删 Discord 频道 → 清持久化
```

## Workspace 初始化顺序

启动时确保 workspace 在 backend 之前就绪：

```
1. _ensure_git_repo()      → 创建目录 + .git
2. _sync_skills()          → 同步 skill symlinks
3. seed_*_workspace()      → 写入模板文件
4. cli.start()             → 启动 Claude CLI 进程
5. pool.start()            → 启动 session pool
```

## 持久化

| 数据 | 存储位置 |
|------|---------|
| 静态 specialist | config.yaml `workgroups.*.specialists` |
| 动态 specialist | `~/.boxagent/local/workgroup_specialists.yaml` |
| Session ID | `~/.boxagent/local/sessions.yaml`（per chat_id） |
| 对话日志 | `~/.boxagent/local/transcripts/{session_id}.jsonl` |
| Heartbeat 日志 | `{admin_workspace}/heartbeat.log` |

## 测试

```bash
# 单元测试（58 个）— 纯函数和数据逻辑
uv run pytest tests/unit/test_workgroup.py -v

# 集成测试（26 个）— 异步操作 + mocked backends
uv run pytest tests/unit/test_workgroup_integration.py -v
```

覆盖：format_running_tasks, XML 提取, is_silent_reply, heartbeat prompt, template seed, specialist CRUD, heartbeat tick, session fork。

## 限制 / TODO

- 不支持 specialist 之间互相调用（单向：admin → specialist）
- Codex CLI backend 的 MCP 注入还未拆分（当前只有 claude-cli 支持 admin MCP）
- 没有 workgroup 级别的 cost tracking
- heartbeat fork session 在没有历史对话时启动全新 session（能读 CLAUDE.md 但没有上下文）
