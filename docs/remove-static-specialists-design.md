# 移除静态 Specialist 配置 — 设计文档

## 1. 当前实现分析

### 两种 Specialist 来源

| | 静态（config.yaml） | 动态（create_specialist MCP tool） |
|--|-----|------|
| 定义位置 | `config.yaml` → `workgroups.<name>.specialists:` | `workgroup_specialists.yaml`（持久化） |
| 解析 | `config.py:581-613` `_parse_workgroup()` | `manager.py:79-101` `_load_saved_specialists()` |
| 启动行为 | 直接创建 | 从 yaml 恢复后创建 |
| 可删除 | ❌（`_builtin_specialists` 保护） | ✅ |
| 持久化 | 不需要（config.yaml 本身就是持久） | `_save_specialist()` 写入 `workgroup_specialists.yaml` |

### 启动流程（`manager.py:start_workgroup()`）

```
1. self._builtin_specialists[wg_name] = set(wg_cfg.specialists.keys())  # line 208
2. saved = self._load_saved_specialists(wg_name)                         # line 209
3. for sp_name, sp_cfg in saved.items():                                 # line 210-212
       if sp_name not in wg_cfg.specialists:
           wg_cfg.specialists[sp_name] = sp_cfg                          # 合并到 specialists dict
4. for sp_name, sp_cfg in wg_cfg.specialists.items():                    # line 335-341
       self._create_specialist_agent(sp_name, sp_cfg, wg_cfg, dc_channel)
```

关键点：静态和动态最终都合并到 `wg_cfg.specialists` dict，走同一个 `_create_specialist_agent()` 创建路径。区别仅在于 `_builtin_specialists` 记录了哪些是静态的（阻止删除）。

### 涉及 builtin 概念的代码位置

| 文件 | 行 | 用途 |
|------|---|------|
| `manager.py:69` | `_builtin_specialists` 字段定义 | 存储 builtin 集合 |
| `manager.py:208` | `_builtin_specialists[wg_name] = set(wg_cfg.specialists.keys())` | 启动时记录 |
| `manager.py:716-722` | `delete_specialist()` 检查 builtin | 阻止删除 |
| `manager.py:486-514` | `list_specialists()` 标记 `"builtin": True/False` | MCP 展示 |
| `mcp_http.py:235-238` | 格式化输出 "built-in" / "dynamic" | 用户可见 |

### 涉及静态配置解析的代码

| 文件 | 行 | 用途 |
|------|---|------|
| `config.py:47-56` | `SpecialistConfig` dataclass | 数据结构 |
| `config.py:86` | `WorkgroupConfig.specialists` dict | 持有 specialist 列表 |
| `config.py:581-613` | `_parse_workgroup()` 中 specialist 解析循环 | YAML → SpecialistConfig |
| `config.py:97-99` | `WorkgroupConfig.specialist_workspace()` | 派生 workspace 路径 |

---

## 2. 移除方案

### 2.1 核心思路

**动态 specialist 已经有完整的持久化和恢复机制**（`workgroup_specialists.yaml`），启动时自动恢复。移除静态配置后，所有 specialist 都是动态的，启动时从 yaml 恢复，任何时候可增删。

### 2.2 代码变更清单

| 文件 | 变更 | 行数 |
|------|------|------|
| **config.py** | 删除 `_parse_workgroup()` 中 specialist 解析循环（lines 581-613）；`WorkgroupConfig.specialists` 字段保留但启动时为空 dict | -35 |
| **manager.py** | 删除 `_builtin_specialists` 字段 (line 69)；删除 line 208 的 builtin 记录；删除 `delete_specialist()` 中 builtin 检查 (lines 716-722) | -10 |
| **manager.py** | `list_specialists()` 中移除 `builtin` 标记（line 511） | -3 |
| **mcp_http.py** | `list_specialists()` 格式化中删除 "built-in"/"dynamic" 显示（lines 235-238） | -4 |
| **tests** | 删除 builtin 保护相关测试；调整其他测试 fixture | -20 |

**总计：删除约 70 行，修改约 10 行。净减代码。**

### 2.3 保留不变的部分

- `SpecialistConfig` dataclass — 仍需要（动态创建也用）
- `WorkgroupConfig.specialists` dict — 仍需要（运行时存储活跃的 specialist）
- `WorkgroupConfig.specialist_workspace()` — 仍需要（动态创建时派生 workspace 路径）
- `_load_saved_specialists()` — 不变
- `_save_specialist()` — 不变
- `_create_specialist_agent()` — 不变
- `create_specialist()` MCP tool — 不变
- `delete_specialist()` MCP tool — 简化（去掉 builtin 检查）

### 2.4 启动流程变更

**Before:**
```python
# 1. 从 config.yaml 读取静态 specialist
self._builtin_specialists[wg_name] = set(wg_cfg.specialists.keys())
# 2. 从 workgroup_specialists.yaml 恢复动态 specialist
saved = self._load_saved_specialists(wg_name)
for sp_name, sp_cfg in saved.items():
    if sp_name not in wg_cfg.specialists:
        wg_cfg.specialists[sp_name] = sp_cfg
# 3. 创建所有 specialist
for sp_name, sp_cfg in wg_cfg.specialists.items():
    self._create_specialist_agent(...)
```

**After:**
```python
# 1. 从 workgroup_specialists.yaml 恢复所有 specialist
saved = self._load_saved_specialists(wg_name)
wg_cfg.specialists = saved
# 2. 创建所有 specialist
for sp_name, sp_cfg in wg_cfg.specialists.items():
    self._create_specialist_agent(...)
```

### 2.5 `delete_specialist()` 简化

**Before:**
```python
async def delete_specialist(self, sp_name: str) -> dict:
    if sp_name not in self.routers:
        return {"ok": False, "error": f"specialist '{sp_name}' not found"}
    # Check if built-in
    for wg_name, builtin_names in self._builtin_specialists.items():
        if sp_name in builtin_names:
            return {"ok": False, "error": "...built-in...cannot be deleted"}
    ...
```

**After:**
```python
async def delete_specialist(self, sp_name: str) -> dict:
    if sp_name not in self.routers:
        return {"ok": False, "error": f"specialist '{sp_name}' not found"}
    # No builtin check — all specialists are deletable
    ...
```

---

## 3. 迁移策略

### 3.1 向后兼容：config.yaml 还有 `specialists:` 段

**方案：log warning + 忽略。**

```python
# config.py, _parse_workgroup()
if raw.get("specialists"):
    logger.warning(
        "Workgroup '%s': 'specialists' section in config.yaml is deprecated and ignored. "
        "Use create_specialist MCP tool to create specialists dynamically.",
        wg_name,
    )
```

不 crash，不自动迁移（避免副作用），只告知用户。

### 3.2 数据迁移

用户需要做的：
1. 用 `create_specialist` MCP tool 创建之前在 config.yaml 中定义的 specialist
2. 或者手动写 `~/.boxagent/local/workgroup_specialists.yaml`

可以提供一个一次性迁移脚本（可选），但鉴于静态 specialist 数量通常很少（1-5 个），手动创建更实际。

### 3.3 现有的 workgroup_specialists.yaml 是否足够？

**是的。** 已有字段：
```yaml
war-room:
  dev-1:
    model: ""
    workspace: /path/to/workspace
    ai_backend: claude-cli
    display_name: Developer 1
    discord_channel: 123456789
```

与 config.yaml 中的静态 specialist 配置等价。唯一缺少的是 `extra_skill_dirs`。

**需要补充**：`_save_specialist()` 和 `_load_saved_specialists()` 增加 `extra_skill_dirs` 字段的序列化/反序列化。

当前 `_save_specialist()` 保存的字段：
```python
data.setdefault(wg_name, {})[sp.name] = {
    "model": sp.model,
    "workspace": sp.workspace,
    "ai_backend": sp.ai_backend,
    "display_name": sp.display_name,
    "discord_channel": sp.discord_channel,
}
```

需要增加：
```python
    "extra_skill_dirs": sp.extra_skill_dirs,
```

以及 `_load_saved_specialists()` 中：
```python
result[sp_name] = SpecialistConfig(
    ...
    extra_skill_dirs=sp_raw.get("extra_skill_dirs", []),
)
```

### 3.4 create_specialist MCP tool 扩展

当前 `create_specialist` 只接受 `name` 和 `model` 参数。移除静态配置后，需要支持更多参数：

```python
async def create_specialist(
    name: str,
    model: str = "",
    extra_skill_dirs: list[str] | None = None,  # 新增
    display_name: str = "",                      # 新增
) -> str:
```

这样 admin 可以完全通过 MCP tool 控制 specialist 的所有配置。

---

## 4. 风险和注意事项

### 4.1 低风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| 用户 config.yaml 有 specialists 段 | 升级后 specialist 不自动启动 | Warning 日志 + 文档说明迁移步骤 |
| extra_skill_dirs 丢失 | 当前动态 specialist 不保存 skill dirs | 补充序列化（见 3.3） |

### 4.2 需要注意

| 事项 | 说明 |
|------|------|
| **Admin CLAUDE.md 中的 specialist 列表** | `context.py` 的 session context 注入是从 `router.workgroup_agents` 读的（runtime 数据），不是从 config.yaml 读的。移除静态配置不影响——启动恢复后 workgroup_agents 正常填充。 |
| **首次启动（无 workgroup_specialists.yaml）** | Workgroup 启动时没有任何 specialist。Admin 需要手动 `create_specialist`。这是预期行为——"动态优先"。 |
| **Discord channel 管理** | 静态 specialist 的 `discord_channel` 是预先在 config 里写死的 channel ID。动态创建时 channel 是自动创建的。迁移时用户需要决定：用新 channel 还是保留旧 channel。如果要保留旧 channel，需要在 `workgroup_specialists.yaml` 中手动填写 `discord_channel: <existing_id>`。 |

### 4.3 测试覆盖

需要更新的测试：
- `test_workgroup_integration.py`：删除 `test_rejects_builtin` 测试
- 其他使用 `_builtin_specialists` fixture 的测试：简化
- 新增：启动恢复测试（从 yaml 恢复后 specialist 正常可用）

---

## 5. 变更总结

| 项目 | 动作 |
|------|------|
| `config.py` 静态 specialist 解析 | 删除，改为 warning |
| `manager.py` `_builtin_specialists` | 删除字段和所有引用 |
| `manager.py` `delete_specialist()` builtin 检查 | 删除 |
| `manager.py` `list_specialists()` builtin 标记 | 删除 |
| `mcp_http.py` "built-in"/"dynamic" 显示 | 删除 |
| `_save_specialist()` / `_load_saved_specialists()` | 增加 `extra_skill_dirs` 序列化 |
| `create_specialist` MCP tool | 扩展参数（extra_skill_dirs, display_name） |
| 测试 | 删除 builtin 测试，简化 fixture |

**净效果（不含模板功能）：删除 ~70 行，新增 ~20 行。代码更简单。**

---

## 6. 自定义模板设计

移除静态配置后，`create_specialist` 成为创建 specialist 的唯一入口。模板系统为 admin 提供"预设配置"能力——admin 不需要每次手写完整的 CLAUDE.md，选个模板就行。

### 6.1 目录结构

```
~/.boxagent/<workgroup-name>/
└── .boxagent-workgroup/
    ├── admin/
    ├── specialists/
    ├── worktrees/
    └── templates/                    ← 新增：workgroup 级模板目录
        ├── planner/
        │   ├── template.yaml         ← 元信息
        │   ├── CLAUDE.md             ← 模板层 prompt
        │   └── skills/              ← 可选：模板自带 skills
        │       └── planning/
        │           └── SKILL.md
        ├── security-reviewer/
        │   ├── template.yaml
        │   └── CLAUDE.md
        └── frontend-dev/
            ├── template.yaml
            ├── CLAUDE.md
            └── skills/
                └── frontend/
                    └── SKILL.md
```

路径计算：`Path(wg_cfg.workgroup_dir) / "templates"`

### 6.2 template.yaml 格式

```yaml
name: security-auditor          # 必填，显示名
description: "安全审查专家"      # 必填，list_templates 展示用
default_model: ""               # 可选，空=继承 workgroup
suggested_role: specialist      # 可选，空=specialist
```

- 字段极少——模板的核心价值在 CLAUDE.md 和 skills/，不在配置
- `suggested_role` 只是建议，admin 在 `create_specialist` 时可以覆盖

### 6.3 CLAUDE.md 三层注入

Specialist 的最终 `.claude/CLAUDE.md` 由三层组成：

```
┌────────────────────────────────┐
│ 系统层（不可覆盖）              │  ← 代码仓库 templates/specialist/CLAUDE.md
│ 通信协议、安全约束、response 格式 │     每次启动覆盖
├────────────────────────────────┤
│ 模板层（用户定义）              │  ← workgroup templates/<name>/CLAUDE.md
│ 角色定义、行为约束、能力边界     │     创建时写入
├────────────────────────────────┤
│ 技能层（skills）               │  ← extra_skill_dirs + template skills/
│ 具体领域知识                    │     symlink 注入
└────────────────────────────────┘
```

**实现方式**：

```python
def seed_specialist_workspace(workspace, sp_name, wg_name, template_dir=None):
    # 1. 系统层：始终写入内置 CLAUDE.md（通信协议等）
    system_content = SPECIALIST_CLAUDE_MD.format(...)

    # 2. 模板层：如果有模板，追加其 CLAUDE.md 内容
    if template_dir:
        template_claude = (Path(template_dir) / "CLAUDE.md").read_text()
        system_content += f"\n\n{template_claude}"

    _write_always(ws / ".claude" / "CLAUDE.md", system_content)
```

**关键约束**：系统层在前，模板层追加。模板无法覆盖系统层的 `<specialist_response>` 协议等核心规则。

### 6.4 内置模板 vs Workgroup 模板

| 层级 | 位置 | 用途 | 分发方式 |
|------|------|------|---------|
| **内置** | `src/boxagent/workgroup/templates/` | admin、specialist 的基础模板 | 随代码发布 |
| **Workgroup 级** | `.boxagent-workgroup/templates/` | 用户自定义角色模板 | 用户手动创建或 admin 动态写入 |

**优先级规则**：
- 指定 `template="planner"` → 先找 workgroup 目录，没有则找内置目录
- 指定但两处都没有 → **报错**（不静默 fallback）
- 不指定 template → 用内置 specialist 默认模板

### 6.5 `list_templates()` MCP tool

新增到 `/mcp/admin` 端点：

```python
@mcp.tool()
def list_templates() -> str:
    """List available specialist templates for this workgroup.

    Templates define pre-configured roles with custom CLAUDE.md prompts
    and optional skill directories.
    """
    templates = []

    # 1. 内置模板
    builtin_dir = Path(__file__).parent / "templates"
    for d in sorted(builtin_dir.iterdir()):
        if d.is_dir() and (d / "CLAUDE.md").exists() and d.name != "admin":
            templates.append({"name": d.name, "source": "builtin", "description": ""})

    # 2. Workgroup 级模板（覆盖同名内置）
    wg_templates_dir = Path(wg_cfg.workgroup_dir) / "templates"
    if wg_templates_dir.is_dir():
        for d in sorted(wg_templates_dir.iterdir()):
            if not d.is_dir() or not (d / "CLAUDE.md").exists():
                continue
            meta = {}
            if (d / "template.yaml").exists():
                meta = yaml.safe_load((d / "template.yaml").read_text()) or {}
            # 覆盖同名内置
            templates = [t for t in templates if t["name"] != d.name]
            templates.append({
                "name": d.name,
                "source": "workgroup",
                "description": meta.get("description", ""),
                "suggested_role": meta.get("suggested_role", "specialist"),
                "default_model": meta.get("default_model", ""),
            })

    return format_templates(templates)
```

### 6.6 `create_specialist` 扩展

完整新签名：

```python
async def create_specialist(
    name: str,
    model: str = "",
    template: str = "",              # 新增：模板名（查找 workgroup → 内置）
    role: str = "specialist",        # 新增：角色（影响 MCP 权限）
    extra_skill_dirs: list[str] | None = None,  # 新增
    display_name: str = "",          # 新增
) -> str:
```

**创建流程**：

```
1. 解析 template → 找到模板目录（workgroup 优先，找不到报错）
2. 合并 model：template.default_model < 参数 model
3. 合并 skills：template.skills/ + extra_skill_dirs
4. 创建 workspace 目录 + .git
5. seed_specialist_workspace(template_dir=resolved_template_dir)
6. 启动 backend + pool + router
7. 持久化到 workgroup_specialists.yaml（含 template 名）
```

### 6.7 持久化变更

`workgroup_specialists.yaml` 增加 `template` 和 `role` 字段：

```yaml
war-room:
  planner-1:
    model: opus
    workspace: /path/to/workspace
    ai_backend: claude-cli
    display_name: Planner
    discord_channel: 123456789
    template: planner              # 新增
    role: planner                  # 新增
    extra_skill_dirs:              # 新增
      - /path/to/skills
```

恢复启动时，如果 template 字段存在，重新应用模板的 CLAUDE.md（确保系统层 + 模板层都是最新版本）。

### 6.8 模板 skills/ 目录处理

模板内 `skills/` 目录下的内容自动合并到 specialist 的 `extra_skill_dirs`：

```python
effective_skill_dirs = []
if template_dir and (Path(template_dir) / "skills").is_dir():
    effective_skill_dirs.append(str(Path(template_dir) / "skills"))
if extra_skill_dirs:
    effective_skill_dirs.extend(extra_skill_dirs)
```

Skill dirs 通过现有的 `_sync_skills()` 机制 symlink 到 specialist workspace。

### 6.9 Bug 修复

**当前 bug**：`create_specialist()` (manager.py:681-688) 创建 SpecialistConfig 时没传 `extra_skill_dirs`：

```python
# 当前代码（有 bug）
sp_cfg = SpecialistConfig(
    name=sp_name,
    model=model or wg_cfg.model,
    workspace=workspace or wg_cfg.specialist_workspace(sp_name),
    ai_backend=wg_cfg.ai_backend,
    display_name=sp_name,
    discord_channel=discord_channel_id,
    # extra_skill_dirs 缺失！
)
```

需要修复为：
```python
sp_cfg = SpecialistConfig(
    name=sp_name,
    model=model or wg_cfg.model,
    workspace=workspace or wg_cfg.specialist_workspace(sp_name),
    ai_backend=wg_cfg.ai_backend,
    display_name=display_name or sp_name,
    discord_channel=discord_channel_id,
    extra_skill_dirs=effective_skill_dirs,  # 修复
)
```

---

## 7. 更新后的实现估算

| 项目 | 改动量 |
|------|--------|
| **移除静态配置**（§2） | 删 70 行，改 10 行 |
| **自定义模板发现逻辑** | +40 行（`list_templates` + 模板目录扫描） |
| **`create_specialist` 扩展** | +30 行（参数处理、模板解析、skill 合并） |
| **`seed_specialist_workspace` 改造** | +20 行（支持 template_dir 参数、三层注入） |
| **持久化扩展**（template/role/extra_skill_dirs） | +15 行 |
| **`list_templates` MCP tool** | +25 行 |
| **Bug 修复**（extra_skill_dirs） | +2 行 |
| **测试** | +50 行（模板发现、创建、持久化恢复） |

**总计：删 70 行，新增 ~180 行。** 净增约 110 行，换来完整的模板系统 + 移除静态配置的简化。
