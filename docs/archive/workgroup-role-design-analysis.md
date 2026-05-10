# Workgroup Role 体系设计 — 综合分析

**Status: Revision 2 — Approved**
**参与方:** devbox-xl, medium, mbp-old, mac-mini (design-lead)

---

## 0. 修订历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-04-29 | 初版：提出 admin/lead/reviewer/specialist 四角色方案 |
| **v2** | **2026-04-29** | **Bill 否决 v1，砍掉 Lead，新增 Planner。大幅简化。** |

### v1 被否决的原因

Bill 的核心反馈：**Admin 和 Lead 权限完全重复**——两者都能 `send_to_agent`、`list_specialists`、`cancel_task`，唯一区别是 Lead 不能 create/delete specialist。这不值得为此引入新角色、拆分 MCP 端点、增加 300 行代码。

实际场景中 Admin 完全有能力做任务拆解和委派，不需要中间层。真正缺失的是**规划能力**——一个专门做研究和产出设计文档的角色。

---

## 1. 现有架构摘要

当前系统只有两个角色：

| 角色 | 权限边界 | 实现方式 |
|------|---------|---------|
| **admin** | `boxagent-admin` MCP 端点（list/create/delete/send_to specialist） | `workgroup_role="admin"` → `claude_process.py:105` 注入 `/mcp/admin` |
| **specialist** | 无管理工具，只能被动接收任务 | 不设 `workgroup_role`（默认 `""`），MCP 只注入 `/mcp/base` |

### 关键实现细节

1. **权限边界 = MCP 端点注册**：`claude_process.py:98-124` 根据 `AgentEnv` 的 property 决定注入哪些 MCP server。新角色 = 新的 MCP 端点组合。

2. **Specialist 调用机制**：Admin 通过 `send_to_agent(name, message)` MCP tool 派任务给 specialist。Specialist 执行后结果自动回调给 admin（`manager.py` 中 `send_to_specialist()` → `dispatch_sync()` → 提取 `<specialist_response>` → 回注给 admin router）。

3. **Specialist 行为由 template 决定**：`workspace_templates.py` 的 `seed_specialist_workspace()` 写入 CLAUDE.md 和 SKILL.md，定义 specialist 的行为规范。不同的 template = 不同的行为约束。

4. **SpecialistConfig 当前无 role 字段**：`config.py:47-56` 只有 name/model/workspace/ai_backend/display_name/discord_channel/extra_skill_dirs。

5. **MCP 端点是 all-or-nothing 的**：`mcp_http.py:573-575` 只有一个 `/mcp/admin`，包含 7 个 tool 全部打包给 admin。

---

## 2. v2 方案：Planner 角色

### 2.1 角色体系

```
admin (L0)      — 调度 + 管理 + 验收（现有不变）
├── planner     — 只读研究 + 产出 PRD/任务拆解/测试方案
└── specialist  — 执行层（现有不变）
```

**3 个角色**。v1 中的 Lead 砍掉，Reviewer 保留为 Phase 2。

### 2.2 Planner 的本质

**Planner 是一个行为受限的 specialist。** 它通过现有 `send_to_agent` 机制调用，结果通过现有 `<specialist_response>` 协议回调给 admin。与普通 specialist 的唯一区别是 **CLAUDE.md template 不同**——限制其只做研究和文档产出，不写代码。

这意味着：
- **零架构改动**：不需要新 MCP 端点、不需要拆分现有端点、不需要改 Router 逻辑
- **复用所有现有机制**：send_to_agent、dispatch_sync、specialist_response 提取、Discord 频道显示
- **行为约束由 prompt 实现**：CLAUDE.md template 限定权限范围

### 2.3 Planner 权限

| 能力 | admin | planner | specialist |
|------|:-----:|:-------:|:----------:|
| 接收用户消息 | ✅ | ❌ | ❌ |
| send_to_agent（派任务） | ✅ | ❌ | ❌ |
| create/delete specialist | ✅ | ❌ | ❌ |
| 读代码（Read/Grep/Glob） | ✅ | ✅ | ✅ |
| git 只读（log/blame/diff） | ✅ | ✅ | ✅ |
| 写文件（docs/ 目录） | ✅ | ✅ | ✅ |
| 编辑非 docs/ 文件 | ✅ | ❌ | ✅ |
| 执行 build/test 命令 | ✅ | ❌ | ✅ |
| git commit/push | ✅ | ❌ | ✅ |
| peer messaging | ✅ | ❌ | ❌ |
| heartbeat | ✅ | ❌ | ❌ |
| schedule/sessions tools | ✅ | ✅ | ✅ |

**关键区别**：Planner 不能改代码、不能跑测试、不能 commit。它只能读和写文档。

### 2.4 Planner 工作流

```
1. User/Admin 识别需要规划的任务
2. Admin 调用 send_to_agent("planner", "研究 X 模块，产出 PRD + 任务拆解")
3. Planner:
   a. 研究代码（Read/Grep/Glob/git log）
   b. 分析架构和依赖
   c. 产出文档（写入 docs/ 目录或直接在 response 中返回）
   d. 回复 <specialist_response>PRD + 任务拆解 + 测试方案</specialist_response>
4. 结果自动回调给 Admin
5. Admin 据此创建专家团队或派任务给现有 specialist 执行
```

### 2.5 Planner 产出物示例

- PRD（需求文档）
- 任务拆解（带优先级和依赖关系）
- 测试方案（测试用例设计）
- 架构分析报告
- 代码影响面分析

### 2.6 为什么用高模型（opus）

规划质量直接决定执行质量。Planner 的输出是其他 specialist 的输入——如果规划有误，执行阶段的返工成本远高于规划阶段用好模型的成本。建议默认配置 `model: opus`。

---

## 3. 配置结构

```yaml
workgroups:
  war-room:
    # ... existing fields unchanged ...

    specialists:
      planner:
        role: planner              # 新 role 值，决定使用 planner template
        model: opus                # 建议用高模型
        display_name: Planner
        # 无需 manages、无需额外 MCP 端点
        # 行为限制完全由 template 控制

      dev-1:
        # role 省略 = "specialist"（默认值）
        display_name: Developer 1

      dev-2:
        display_name: Developer 2

      ops-1:
        display_name: Ops Engineer
        extra_skill_dirs: [./skills/ops]  # 用 skill 差异化
```

**新增配置字段：**

| 字段 | 所属 | 类型 | 默认值 | 说明 |
|------|------|------|--------|------|
| `role` | SpecialistConfig | `str` | `"specialist"` | 可选值：`"specialist"`, `"planner"` (Phase 2: `"reviewer"`) |

其他字段不变。不需要 `manages`、`peer_workgroups` 等 v1 中提出的字段。

---

## 4. 实现方案

### 4.1 代码变更清单

| 文件 | 变更内容 | 估算行数 |
|------|---------|---------|
| `config.py` | `SpecialistConfig` 增加 `role: str = "specialist"`；解析时校验 role 值合法 | ~10 |
| `workspace_templates.py` | 新增 `PLANNER_CLAUDE_MD` / `PLANNER_SKILL_MD` 加载；新增 `seed_planner_workspace()` 函数；在 `manager.py` 调用处按 role 选择 seed 函数 | ~30 |
| `templates/planner/` | 新目录：`CLAUDE.md`（核心：限制只读+写docs）、`SKILL.md`（规划方法论）、`templates.md`（PRD/任务拆解模板） | 新文件 |
| `manager.py` | `_create_specialist_agent()` 中根据 `sp_cfg.role` 选择 `seed_planner_workspace` vs `seed_specialist_workspace` | ~5 |
| 测试 | planner template seeding 测试 + role 配置解析测试 | ~30 |

**总计约 50 行代码改动 + template 文件。** 零架构变更。

### 4.2 Planner Template 设计（`templates/planner/CLAUDE.md`）

核心内容：

```markdown
# Planner — {sp_name}

> Workgroup: {wg_name}

You are a **planning specialist**. Your job is to research, analyze, and
produce design documents. You do NOT write implementation code.

## What you CAN do
- Read any file in the codebase (Read, Grep, Glob tools)
- Run git read-only commands (git log, git blame, git diff, git show)
- Write files ONLY in docs/ directories
- Produce PRDs, task breakdowns, test plans, architecture analysis

## What you MUST NOT do
- Edit source code files (*.py, *.ts, *.js, *.go, etc.)
- Run build or test commands (make, npm, pytest, cargo, etc.)
- Create git commits or push branches
- Create/delete other agents
- Execute destructive shell commands

## Output format
Always wrap your final deliverable in <specialist_response> tags.
Structure your output as actionable documents that an execution team
can directly work from.
```

### 4.3 不需要的变更

以下是 v1 方案中需要但 v2 **不再需要**的改动：

| v1 计划的变更 | 为什么不需要了 |
|--------------|---------------|
| 拆分 `/mcp/admin` 为 delegation/management/readonly | Planner 不需要任何 admin tool |
| `agent_env.py` 增加 `can_delegate` 等 property | Planner 不做委派 |
| `claude_process.py` 按 role 组合 MCP 端点 | Planner 只用 `/mcp/base`（同普通 specialist） |
| Lead template（介于 superboss 和 supercrew 之间） | 不需要 Lead 角色 |
| Reviewer readonly MCP 端点 | Phase 2 |
| `peer_workgroups` 跨 workgroup 通信 | Phase 2+ |
| `manages` 字段 | 不需要 Lead 角色 |

### 4.4 向后兼容性

| 变更 | 兼容？ | 说明 |
|------|:------:|------|
| `role` 字段 | ✅ | 默认 `"specialist"`，省略则行为完全不变 |
| 新 template 目录 | ✅ | 只在 `role=planner` 时使用 |
| `manager.py` 条件分支 | ✅ | 无 planner 配置时走原有 specialist 路径 |

---

## 5. MCP 端点（不变）

v2 方案下 MCP 端点结构**完全不动**：

| 端点 | 包含 tools | 注入给 |
|------|-----------|--------|
| `/mcp/base` | schedule_*, sessions_list | 所有角色（admin, planner, specialist） |
| `/mcp/admin` | list/create/delete/reset specialist, send_to_agent, cancel_task, update_channel_topic | admin only |
| `/mcp/peer` | send_to_peer | admin（按配置） |
| `/mcp/telegram` | send_photo/video/doc/audio/animation | 按配置 |

Planner 和普通 specialist 一样，只获得 `/mcp/base`。行为差异完全由 template 控制。

---

## 6. 实现优先级

| 优先级 | 内容 | 复杂度 |
|--------|------|--------|
| **P0** | Planner 角色（config + template + manager 分支） | 极小（~50 行 + template） |
| **P1** | Reviewer 角色（Phase 2：需要决定反馈机制） | 小 |
| **P2** | MCP 端点拆分（如果未来需要更细粒度权限） | 中 |

---

## 7. Phase 2：Reviewer（未来扩展）

保留 reviewer 角色的设计方向，但不在 v1 实现：

- **定位**：审查 specialist 产出，通过 peer channel 反馈
- **权限**：只读 + peer messaging，不能派任务
- **触发**：Admin 通过 `send_to_agent("reviewer", "审查 dev-1 的 PR")` 调用
- **反馈**：通过 `/mcp/peer` 的 `send_to_peer` 发布审查结果
- **结构化协议**：`<review_result>` XML 标签（verdict/summary/details）
- **前置依赖**：需要 reviewer 能读取其他 specialist 的工作成果（可能需要 `/mcp/readonly` 端点，或直接通过 git 读取）

---

## 8. 设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| Planner vs Lead | Planner | Admin 已有全部委派能力，不需要中间层。缺的是规划能力。 |
| Planner 实现方式 | Template 约束 | 零架构改动，最小实现成本 |
| 权限控制方式 | Prompt-based（CLAUDE.md） | Planner 的限制（不改代码、不跑测试）不需要代码级强制，prompt 约束足够 |
| Coordinator | 不要 | 跨 workgroup 协调是能力不是角色（三方共识） |
| Specialist 拆分 | 不拆 | 通过 skill 配置差异化（三方共识） |
| MCP 端点拆分 | 推迟到 Phase 2 | Planner 不需要，暂无其他角色需要 |
| Planner 默认模型 | opus | 规划质量比执行速度重要 |

---

## 9. 开放问题

1. **Planner 的 yolo 配置？** Planner 理论上不需要 yolo（不执行危险操作）。但 Claude Code 的 yolo 模式影响所有 tool permission，建议 planner 配置 `yolo: false` 以增加安全性。

2. **Planner 能否写入非 docs/ 目录？** 当前设计是 template 约束"只写 docs/"，但技术上 Planner 仍有 Write tool 权限。如果需要硬限制，需要 Claude Code 的 `allowedTools` 配置——但这超出当前 box-agent 控制范围。建议 v1 靠 prompt 约束，观察实际行为后决定是否需要加强。

3. **Planner 的 worktree 使用？** Planner 是只读的，不需要 worktree 隔离。Template 中应明确说明"不要创建 worktree"。

4. **多个 Planner 实例？** 配置上可以创建多个 planner（如 `planner-arch`、`planner-test`），各有不同 extra_skill_dirs。这是自然支持的，无需额外代码。
