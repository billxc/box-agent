# Transports（Channels）

> 依据源码：`src/boxagent/transports/`。Router 只透过 `Channel` Protocol 跟 channel 打交道。

## Channel Protocol（`transports/base.py`）

入站：channel 自己起监听（Telegram 轮询 / Web SSE inject），收到消息调
`self.on_message(IncomingMessage)`（`AgentManager.start_bot` 把它接到 `router.handle_message`）。
出站：Router 用 `send_text` / `stream_start|update|end` / `show_typing` / `on_tool_call` /
`on_tool_update` 把内容推回去。

两个核心数据类：

- **`IncomingMessage`**（`base.py:24`）：`channel` / `chat_id` / `user_id` / `text` / `attachments` / `reply_to` / **`trusted`** / `timestamp` / `channel_info`。
- **`StreamHandle`**（`base.py:38`）：`message_id` / `chat_id` / `webhook_name` —— 一次流式消息的句柄。

流式契约：`stream_update(handle, text)` 传的是**增量 chunk**（不是累积全文），各 channel 自己累积。

## Telegram（`transports/telegram/`）

- `channel.py:TelegramChannel` —— aiogram 3，`start()` 起 `Bot` + `Dispatcher` 长轮询，并把 slash 命令注册进 Telegram 菜单。
- **流式编辑**：缓冲 + 节流（`THROTTLE_MS=300`、`FLUSH_CHAR_THRESHOLD=200`），edit-in-place。`TELEGRAM_LIMIT=4096`，流式超过 `STREAM_SPLIT_THRESHOLD=3800` 自动翻页（`splitter.py`）。
- `md_format.py:md_to_telegram` —— Markdown → Telegram MarkdownV2（转义规则）。
- `splitter.py:split_message` —— 长消息按段落/代码块边界拆分。

## Web UI（`transports/web/`）——默认开启

### 后端

- **`channel.py:WebChannel`** —— **publish-only** 的 in-process channel。把 assistant 消息 / 流式 delta / tool card / typing 事件 `publish` 到共享 MessageBus 的 `chat.<machine_id>.<bot>.<chat_id>` topic（`_publish`，`channel.py:61`）。浏览器通过 SSE `bus.subscribe` 同一 topic 收。WebChannel 自己不持队列。
  - `inject()`（`channel.py:175`）= 入站：HTTP `/api/send` 调它，造 `IncomingMessage(channel="web", trusted=True)` 并 **`asyncio.create_task(on_message)`**（不阻塞，否则跨机中继 30s 上限会 504 丢整轮）。
  - ⚠️ Web 消息 `trusted=True` → **绕过 `allowed_users`**；Web 的准入靠 `web_token`（见下），不是 per-bot allowed_users。
- **`server.py:WebHttpServer`** —— **Starlette + Hypercorn（HTTP/2）**（`gateway.py` 装配，默认端口 9292，`web_host` 默认 `127.0.0.1`，`0.0.0.0` 才能手机/局域网访问）。同时挂 Web UI 和 cluster guest WS。

主要路由（`server.py:198` 起，全部核对过）：

| 路由 | 用途 |
|---|---|
| `GET /` | Web UI 页面 |
| `GET /api/bots` · `GET /api/machines` | bot 列表 / cluster 机器拓扑 |
| `GET /api/sessions` · `POST /api/sessions/rename` · `GET /api/session_info` | 会话列表 / 改名 / 详情 |
| `GET /api/version` | 版本（cluster fast-fail 握手也用它） |
| `POST /api/admin/restart` · `POST /api/admin/cluster_restart` | 重启本机 / 全网 |
| `GET /api/history` · `POST /api/send` · `GET /api/stream` · `WS /api/multiplex` | 历史 / 发消息 / SSE 流 / WebSocket 多路复用 |
| `GET/POST /api/claude/{projects,sessions,transcript,resume}` | Claude 原生 session 浏览/恢复 |
| `/api/events` · `/api/events/stream` · `/api/events/categories` · `/api/events/machines` · `/api/events/{id}/read` · `/api/events/read_all` | Events 页 |
| `GET /api/logs` | Logs 页（`log_file.py:read_tail` 反向分页 + level/grep 过滤） |
| `GET /api/schedules` · `GET /api/schedules/runs` | Schedules 页 |
| `/api/guest/ws` | cluster guest WebSocket（`ClusterHttpRoutes` 注入） |

跨机的 `/api/*` 读写（别机的 sessions/history/events/schedules）由 `RequestReply.dispatch_machine_request`
路由到目标机（本机 in-process / 跨机经 host 中继）——见 [Cluster](cluster.md)。

### 前端（`web/static/`，纯 vanilla HTML/CSS/JS，无框架）

- 四个页面：`index.html`（Chat）/ `events.html` / `logs.html` / `schedules.html`，统一 top-left 导航。
- 逻辑：`app.js`（主入口）、`chat-controller.js`（SSE 流控制）、`multiplex.js`（WS 多路复用）、`session-data.js`、`sidebar-resize.js`、`events.js` / `logs.js` / `schedules.js`、`theme.js`。
- Web components（`components/`）：`chat-log` / `chat-message` / `tool-card` / `machines-panel` / `recents-panel` / `sessions-panel` / `session-picker` / `session-info` / `recap-banner`。
- **主题系统**：shape × palette 两轴（brutalist/phosphor/ink/soft/… × amber/matrix/synthwave/nord/gruvbox/mono/…）。CSS 拆 `style.css` + `events.css` + `*.themes.css`。
- **前端有自动测试**：`static/test/*.test.js`，用 `node --test` + 自写 DOM stub（无 jsdom）。Python 侧 `tests/unit/test_web_frontend.py` / `test_web_static_assets.py` 守着。

### iOS app（`ios/BoxAgent/`）

Swift app，**不是独立 transport**：消费 Web 的 `/api/sse`（SSE）+ `/api/history` + `/api/sessions`，
等价一个移动 Web client。本 wiki 不展开其实现。

## MCP（`transports/mcp/server.py`）

给 **CLI 类 backend** 暴露 BoxAgent 工具的 HTTP MCP server（streamable-http，uvicorn，端口写
`mcp-port.txt`）。两个 endpoint（path → group）：

- `/mcp/base` → group `"base"`（schedule / sessions 工具）
- `/mcp/telegram` → group `"telegram"`（媒体工具，仅有 telegram channel 的 bot）

per-request 上下文（bot_name / chat_id）走 HTTP header `X-BoxAgent-Bot-Name` / `X-BoxAgent-Chat-Id`
→ ContextVar（`_ContextMiddleware`）。registry → FastMCP 转换在 `tools/adapters/mcp_http.py`。

> 现实：`claude-cli` 已重定向到 SDK backend（走 in-process MCP，不碰这个 HTTP server），所以
> **这个 HTTP MCP server 主要服务 `codex-cli`**。SDK 后端用各自的 in-process adapter
> （`claude_sdk.py` / `copilot_sdk.py`）。已知坑：`mcp-port.txt` 被外部清掉会让 codex-cli 静默无 MCP。

## 加一个 channel

实现 `Channel` Protocol（`send_text` / `stream_*` / `on_tool_*` / `start` / `stop`），照
`telegram/channel.py` 或 `web/channel.py` 抄；在 `AgentManager.start_bot` 里挂上去，把
`channel.on_message = router.handle_message`，并注册进 `router._channels[<channel名>]`
（Router 按 `IncomingMessage.channel` 字符串路由回复）。详见 [扩展点](extending.md)。
