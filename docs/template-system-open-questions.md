# 模板系统 — 待讨论问题清单

Part 2（自定义模板系统）的开放设计问题。Part 1（移除静态 specialist）已在另一边推进，本文件只追踪模板相关的未决项。

参考文档：`remove-static-specialists-design.md` §6

---

## 指导原则

- **重启透明**：进程重启对用户视角无感，specialist、workspace、CLAUDE.md 内容都保留。
- **不刻意清除用户数据**：删除只发生在用户显式 `delete_specialist` 时。
- **模板两层**：系统层（代码内置） + 模板层（用户定义）。无实例层。
- **CLAUDE.md 是创建时快照**：模板源后续修改不影响已存在的 specialist。
- **skills 是 live 引用**：symlink 到模板源，agent 可能更新 skill 内容。

---

## 已决问题

### Q1. CLAUDE.md 实例层 ✅ closed
**决策**：不引入。两层（系统 + 模板）足够。specialist 即用即弃，差异化通过创建独立 specialist 表达。

### Q2. 模板更新如何传播 ✅ closed
**决策**：不传播。模板是创建时快照，已存在的 specialist 不受模板源修改影响。想用新模板 → 显式 `delete + create`。重启不重读模板。

### Q3. `list_templates` 输出格式 ✅ closed
**决策**：简洁列表，只输出 `name: description`。不带其他元信息。

```
Available templates:
- planner: 任务拆解、依赖分析
- security-auditor: 安全审查与漏洞分析
```

### Q4. 模板内 `skills/` 处理 ✅ closed
**决策**：symlink（live 引用）。模板 skills 优先于 `extra_skill_dirs`。理由：skill 是知识载体，agent 可能自行更新。

### Q5. 模板找不到的行为 ✅ closed
**决策**：仅在 `create_specialist` 时校验，找不到 fail loud。重启时不再读模板，不会触发此校验。

### Q6. 模板版本 / 创建时快照标记 ✅ closed
**决策**：不做。无版本概念，breaking 改动 → 删 specialist 重建。

### Q7. 模板元信息文件 ✅ closed
**决策**：
- 文件名：`description.md`（纯 markdown，无 yaml）
- 内容：模板的一行描述（用于 `list_templates`）
- 必需：每个模板目录必须有 `description.md`，缺失则启动 fail loud
- 模板名 = 目录名（唯一真相，无单独 name 字段）

```
任务拆解、依赖分析
```

### Q8. 内置 vs workgroup 模板优先级 ✅ closed
**决策**：强制不同名。启动扫描时发现重名直接报错。无 shadow 概念。

### Q9. 创建后能否切换模板 ✅ closed
**决策**：不支持。`delete + create` 即可。

### Q10. 模板 CLAUDE.md 变量替换 ✅ closed
**决策**：不支持。模板纯静态文本。运行时信息（specialist_name 等）由系统层 CLAUDE.md 注入。

### Q11. `delete_specialist` 删除范围 ✅ closed
**决策**：全删。包括 router/backend、workspace 目录、Discord channel、yaml 条目。显式调用 = 干净删除。

### Q（默认模板）. `create_specialist` 不传 template ✅ closed
**决策**：用现有内置默认 specialist 模板（系统层 + 通用 specialist 层）。等同当前行为。

---

## 最终设计快照

### 目录结构
```
~/.boxagent/<workgroup-name>/
└── .boxagent-workgroup/
    └── templates/
        ├── planner/
        │   ├── description.md         # 一行描述，必需
        │   ├── CLAUDE.md              # 模板层 prompt，必需
        │   ├── skills/                # 可选，每个子目录单独 symlink 到 specialist
        │   │   └── planning/
        │   │       └── SKILL.md
        │   ├── extra_skill_dirs.txt   # 可选，外部 skill 目录清单
        │   ├── extra_skill_allows.txt    # 可选，只允许这些 skill 名（与 blocklist 互斥）
        │   └── extra_skill_blocks.txt    # 可选，排除这些 skill 名（与 allowlist 互斥）
        └── security-auditor/
            ├── description.md
            └── CLAUDE.md
```

模板名 = 目录名（唯一标识）。内置和 workgroup 模板强制不同名，重名启动报错。

### `extra_skill_dirs.txt` 格式
```
# 一行一个目录路径，# 开头是注释
shared-skills/owasp
shared-skills/checklist
/Users/xiaocw/code/some-other-skills
```

- 相对路径锚定到 boxagent config 目录（`~/.boxagent/`），跟现有 `extra_skill_dirs` 规则统一
- 绝对路径直接用
- 每行的目录下每个子目录都是一个 skill，跟 `skills/` 注入语义一致
- 空行和 `#` 开头的行忽略
- 路径不存在 → fail loud（创建 specialist 时报错）

### `extra_skill_allows.txt` / `extra_skill_blocks.txt` 格式
```
# 一行一个 skill 名（即 skill 子目录的最后一段名字）
planning
task-decomp
review
```

**过滤规则**：
- 只过滤 `extra_skill_dirs.txt` 列出来的 skill；模板自带 `skills/` 不受影响
- allowlist 和 blocklist 互斥，同时存在 → fail loud
- 列表里写了不存在的 skill 名 → 静默忽略
- 按子目录最后一段名字匹配（不是完整路径）

### `create_specialist` 签名
```python
async def create_specialist(
    name: str,
    model: str = "",
    template: str = "",                          # 模板名，空=内置默认
    extra_skill_dirs: list[str] | None = None,
    display_name: str = "",
) -> str
```

### 持久化（`workgroup_specialists.yaml`）
```yaml
war-room:
  planner-1:
    model: opus
    workspace: /path/to/workspace
    ai_backend: claude-cli
    display_name: Planner
    discord_channel: 123456789
    template: planner              # 元信息记录，启动不读，list_specialists 展示用
    extra_skill_dirs:
      - /path/to/skills
```

`template` 字段在 `list_specialists` 输出中展示，让 admin 知道每个 specialist 的"出身"。模板被改名/删除后该字段成为 stale 历史记录，原样展示，不清理（符合"不刻意清除用户数据"原则）。

### CLAUDE.md 拼接（创建时）
```
[系统层 SPECIALIST_CLAUDE_MD.format(...)]

[模板层：模板目录的 CLAUDE.md 原文]
```

写入 `<workspace>/.claude/CLAUDE.md`。重启不重写。

### Skills 注入（创建时）
```
effective_skill_dirs = []

# 1. 模板自带 skills/（不受过滤影响）
if template has skills/:
    for each subdir in template/skills/:
        symlink subdir → specialist workspace

# 2. 模板的 extra_skill_dirs.txt（受 allow/block 过滤）
if template has extra_skill_dirs.txt:
    allow = read extra_skill_allows.txt if exists
    block = read extra_skill_blocks.txt if exists
    assert not (allow and block)  # fail loud
    for each path in extra_skill_dirs.txt (resolved against ~/.boxagent/):
        for each subdir in path:
            name = subdir.name
            if allow and name not in allow: skip
            if block and name in block: skip
            symlink subdir → specialist workspace

# 3. create_specialist 用户传入的 extra_skill_dirs（不受过滤影响）
extend with extra_skill_dirs
```

每个 skill 子目录单独 symlink。删除 specialist 时连同 workspace 一起删，模板源不受影响。

### 新增 MCP tool
- `list_templates() -> str`：返回内置 + workgroup 合并后的简洁 `name: description` 列表
