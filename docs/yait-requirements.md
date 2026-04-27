# YAIT 需求报告 — BoxAgent Workgroup 集成

## 背景

BoxAgent workgroup 使用 YAIT 作为 issue tracker。Admin 和 specialist agents 通过 CLI 管理任务。当前 YAIT 缺少 workgroup 所需的关键功能。

## 现有 API 审计

### 满足需求的

| 命令 | 用途 | 状态 |
|------|------|------|
| `init` | 初始化项目 | ✅ |
| `new` | 创建 issue（支持 type/priority/label/assign/milestone/body） | ✅ |
| `list` | 列出 issues（支持过滤、JSON 输出、多种格式） | ✅ |
| `show` | 查看 issue 详情（支持 JSON） | ✅ |
| `close` | 关闭 issue | ✅ |
| `reopen` | 重新打开 issue | ✅ |
| `edit` | 编辑 issue（title/type/priority/assign/body/milestone） | ✅ |
| `assign` / `unassign` | 分配/取消分配负责人 | ✅ |
| `comment` | 添加评论 | ✅ |
| `search` | 全文搜索 | ✅ |
| `label add/remove` | 标签管理 | ✅ |
| `milestone` | 里程碑管理 | ✅ |
| `link` | issue 间关联（blocks/depends-on/relates-to） | ✅ |
| `doc` | 文档管理 | ✅ |
| `bulk` | 批量操作 | ✅ |
| `log` | 变更历史 | ✅ |
| `stats` | 统计 | ✅ |
| `export/import` | 导入导出 | ✅ |

### 缺失的核心功能

#### 1. `status` 字段 — **最关键**

**需求：** Workgroup 需要 6 个状态来跟踪任务生命周期：

```
Backlog → Ready → In Progress → In Review → Done → Archive
```

**当前：** YAIT 只有 `open`/`closed` 两个状态，通过 `close`/`reopen` 切换。

**建议 API：**

```bash
# 查看/修改状态
yait status <ID> [new-status]

# 例子
yait status 3                     # 显示当前状态
yait status 3 in-progress         # 改为 in-progress
yait status 3 done                # 改为 done

# 按状态过滤
yait list --status in-progress
yait list --status ready

# 批量修改状态
yait bulk status in-progress 1 2 3
```

**状态应可配置（per-project）：**

```bash
yait config set workflow.statuses "backlog,ready,in-progress,in-review,done,archive"
```

默认可以是 `open,closed`（向后兼容），workgroup 项目配置自己的 workflow。

`close` 和 `reopen` 应该映射到 workflow 中的状态（比如 `close` = 移到 `done`，`reopen` = 移回 `backlog`）。

#### 2. `update` 命令 — 统一修改入口

**需求：** 模板里写了 `yait update <ID> -s in-progress`，但不存在。

**建议：** `edit` 已经能改 title/type/priority/assign/body/milestone，只需加 `-s/--status` 参数：

```bash
yait edit <ID> -s in-progress
yait edit <ID> -s ready -a dev-1
```

或者新增 `update` 作为 `edit` 的别名（agent 更自然地说 "update"）。

#### 3. 看板视图 — 可选但有用

**需求：** Admin 需要一眼看到所有状态的 issue 分布。

**建议 API：**

```bash
yait board
# 输出：
# Backlog (2)    Ready (1)    In Progress (3)    In Review (1)    Done (5)
# ──────────    ─────────    ───────────────    ──────────────    ────────
# #7 Fix UI     #4 Auth      #1 API refactor    #6 PR review     #2 Setup
# #8 Docs                    #3 Tests                            #5 CI
#                            #9 Deploy
```

## 模板需要修正的命令

模板里写了以下不存在的命令，需要在 YAIT 实现后更新：

```bash
yait update <ID> -s closed       # 不存在 → 应改为 yait edit <ID> -s done 或 yait close <ID>
yait update <ID> -s in-progress  # 不存在 → 应改为 yait edit <ID> -s in-progress
```

## 优先级建议

| 优先级 | 功能 | 工作量 | 理由 |
|--------|------|--------|------|
| **P0** | status 字段 + `yait list --status` 过滤 | 中 | 没有这个，6 列 workflow 无法运作 |
| **P0** | `yait edit -s <status>` | 低 | 加一个参数到已有命令 |
| **P1** | `yait board` 看板视图 | 中 | admin 需要全局视图 |
| **P2** | 可配置 workflow（per-project statuses） | 中 | 不同项目可能需要不同流程 |
| **P2** | `update` 作为 `edit` 别名 | 低 | 语义更自然 |

## 临时方案

在 YAIT 实现 status 之前，可以用 label 模拟：

```bash
yait label add 3 in-progress
yait label remove 3 backlog
yait list --label in-progress
```

但这很笨拙（需要先 remove 旧 label 再 add 新 label），建议尽快实现原生 status。
