# 扩展点与配置

> 依据源码：`router/commands/`、`tools/`、`config.py`。四种扩展都有明确的注册机制。

## 加 slash 命令（`router/commands/`）

在 `router/commands/` 下新建 `.py` 文件（名字不以 `_` 开头），用 `@command` 装饰一个自由函数：

```python
from boxagent.router.commands.registry import command, CommandCategory

@command("/foo", help="my command", category=CommandCategory.TOOLS)
async def cmd_foo(router, msg, channel):     # 固定签名 (router, msg, channel)
    await channel.send_text(msg.chat_id, "hi")
```

- **自动发现**：`router/__init__.py` 启动时 `pkgutil.iter_modules` 遍历 `commands/`，import 触发 `@command` 注册进 `COMMAND_REGISTRY`。丢个文件就生效，无需登记。
- `help` 非空才出现在 `/help`（按 `category` 分组）；`CommandCategory` ∈ `SESSION` / `WORKSPACE` / `TOOLS` / `INFO`（声明顺序即渲染顺序）。
- 重复 `name` 会 `raise`。

**现有命令**（核对 `grep @command`）：

| category | 命令 | 文件 |
|---|---|---|
| SESSION | `/new` `/cancel` `/resume` `/model` `/cd` `/backend` `/compact` | `session.py` |
| WORKSPACE | `/sync_skills` `/sessions` `/trust_workspace` | `workspace.py` |
| TOOLS | `/exec` `/schedule` | `tools.py` |
| INFO | `/status` `/start` `/version` `/verbose` `/help` | `info.py` |

> `/backend` 切 backend kind 会走 `Router.on_backend_switched` → `AgentManager.on_backend_switched`
> 同步 routers/scheduler/watchdog 三处引用。`/compact` 触碰 send/save 顺序，改它跑
> `tests/unit/test_session_chain*.py`。

## 加 MCP 工具（`tools/`）

一份定义、多 backend 分发。在 `tools/builtin/` 下新建文件，用 `@boxagent_tool` 注册：

```python
from boxagent.tools import boxagent_tool, ToolContext

@boxagent_tool(
    name="my_tool",
    group="base",                 # "base" | "telegram" —— 决定挂哪个 MCP endpoint / 能力位
    description="...",
    schema={"arg1": str},
    requires=[],                  # 如 ["telegram"] —— env 满足才暴露
)
async def my_tool(args: dict, ctx: ToolContext) -> str:
    return "result"
```

- **注册**：`tools/builtin/__init__.py` 副作用 import 触发 `@boxagent_tool`。
- `ToolContext`：`bot_name` / `chat_id` / `gateway` / `config_dir` / `local_dir` / `node_id`（adapter 填充，SDK backend 在 session 创建时闭包捕获，MCP HTTP 从 `X-BoxAgent-*` header 取）。
- `tools_for(group, env_caps)` 过滤；`env_capabilities(env)` 把 `AgentEnv` 翻成能力集（`has_telegram` → `"telegram"`）。
- registry `wrapped` 自动记录工具异常/`Error:` 返回到 event log（`Category.AGENT_TOOL_ERROR`），并对 token/password/secret 等 key **脱敏**。

**三个 adapter**（同一 registry → 各 backend 形态，`tools/adapters/`）：

| adapter | 服务 backend | 形态 |
|---|---|---|
| `mcp_http.py` | codex-cli（CLI） | registry → FastMCP HTTP endpoint |
| `claude_sdk.py` | agent-sdk-claude（含 claude-cli） | registry → in-process `SdkMcpServer` |
| `copilot_sdk.py` | agent-sdk-copilot | registry → 原生 Tool 对象列表 |

**现有工具**：base 组 —— `sessions_list`、`log_event`、`schedule_{list,add,logs,show,run,run_detail}`；
telegram 组 —— `send_{photo,document,video,audio,animation}`。

## 加 backend

实现 `agent/protocol.py:AgentBackend` Protocol（结构化，不必继承），在
`agent/backend_factory.py:create_backend()` 加分支，把 kind 加进 `agent/protocol.py:BACKEND_KINDS`。
MCP 挂载按 `agent/mcp_endpoints.py:pick_mcp_endpoints()` 输出自己拼参数（CLI 类可继承
`base_cli.py:BaseCLIProcess` 拿队列/子进程/取消骨架；in-process 类参考 `sdk_claude_process.py`）。
详见 [Agent Backends](agent-backends.md)。

## 加 channel transport

实现 `transports/base.py:Channel` Protocol，照 `telegram/channel.py` 或 `web/channel.py` 抄。
在 `AgentManager.start_bot` 挂上去：`channel.on_message = router.handle_message`，注册进
`router._channels[<channel名>]`（Router 按 `IncomingMessage.channel` 字符串路由回复）。详见
[Transports](transports.md#加一个-channel)。

## 配置结构（`config.py`）

配置目录默认 `~/.boxagent/`，运行时目录默认 `~/.boxagent-local/`。

### `config.yaml`

```yaml
global:
  log_level: info
  web_port: 9292          # web UI 端口
  web_host: 127.0.0.1     # 0.0.0.0 才能手机/局域网访问
  web_token: "..."        # web UI Bearer 鉴权

cluster:                  # 可选；配了才进 cluster 模式
  host: [mbp, devbox-xl, macmini]   # 有序 fallback 列表（bare string 也行）
  tunnel_name: boxagent-cluster
  token: "cluster-shared-secret"    # WS hello 把关

notify:                   # 可选；独立 Telegram 推送器（与 chat bot 解耦）
  telegram:
    token: "..."
    chat_id: "..."
    levels: [error, notify]
    categories: []

bots:
  mybot:
    ai_backend: claude-cli          # claude-cli | codex-cli | agent-sdk-claude | agent-sdk-copilot
    workspace: ~/mywork
    model: sonnet
    enabled_on_nodes: [mbp]         # 空 = 所有 node 都跑
    yolo: false
    channels:
      telegram:
        token: "123:abc"           # 或 bot_id 引 telegram_bots.yaml
        allowed_users: [12345]
      web: true                     # 默认 on；web: false 关掉

node_overrides:           # 可选；按 node_id 深合并覆盖
  mbp:
    global: { web_port: 9393 }
    bots: { mybot: { model: opus } }
```

- **`BotConfig`**（`config.py:22`）：`name` / `ai_backend` / `workspace` / `telegram_token` / `allowed_users` / `model` / `agent` / `extra_skill_dirs` / `display_tool_calls` / `display_name` / `enabled_on_nodes` / `yolo` / `web_enabled` / `passthrough`。
- **`AppConfig`**（`config.py:42`）：node/web/cluster/notify 顶层字段 + `bots`。cluster 的 `host_priority` / `my_host_index` / token 由 `load_config` 从 `cluster:` 块推导。
- 校验硬失败：未知 `ai_backend`、`node_overrides` 非 mapping、telegram `bot_id` 找不到等，`raise ConfigError`。
- 环境变量覆盖：`BOXAGENT_GLOBAL_LOG_LEVEL` / `BOXAGENT_WEB_PORT` / `BOXAGENT_WEB_TOKEN` / `BOXAGENT_CLUSTER_TOKEN` 等。

### `local.yaml`（在 local_dir）

放**本机专属**的 `node_id`（cluster 身份 + `enabled_on_nodes` 匹配的关键）。没配会自动生成
`<hostname>-<hex>` 写回（`config.py:_ensure_default_node_id`）—— 避免"配了一堆 bot 一个没起"的坑。

> **改配置先 grep 源码确认字段名**（项目铁律），别照过时文档猜。
