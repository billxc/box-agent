# Web 前端架构（chat UI）

> 描述 `src/boxagent/transports/web/static/` 下 **chat 页（index.html）** 的架构。
> 无 build step、无框架、无 npm —— 纯 vanilla JS，靠 `<script>` 顺序加载 +
> `window` 全局 + 一个共享 `app` context 组织。
>
> 背景：app.js 曾是 1222 行的大泥球。经过组件化 + 控制器拆分，现在是
> **~300 行的组装根 + 一圈注入式可测控制器 + 一组 callback 风格的 web
> components**。拆分过程见 `decisions.md` 与 PR #24–#31。

## 分层

```
┌─ Layer 4 · 组装根 ────────────────────────────────────────────┐
│  app.js                                                        │
│   • DOM refs + state + 基础 helper (api / curKey / setConn …)   │
│   • 建 app bag → ChatController(app) + MachinesController(app)  │
│   • UI wiring: onclick / collapse / new·rename / menu /        │
│                composer / 键盘 / boot / picker·recents 接线      │
└───────────────┬───────────────────────────────────────────────┘
                │ 注入 & 回挂（app bag = 共享 context）
┌─ Layer 3 · 控制器（app-context，注入式、可测）──────────────────┐
│  chat-controller.js   ChatController(app)                      │
│    switchChat / sendText / openStream / handleEvent /          │
│    loadOlderHistory / refreshSessionInfo / touchSession + 委托  │
│  machines-controller.js  MachinesController(app)               │
│    loadMachines / selectBot / renderMachines / restart* / poll │
└───────────────┬───────────────────────────────────────────────┘
                │ el.render(data,ctx) / el.onX = cb  ↓↑
┌─ Layer 1 · Web Components（custom elements，自管 DOM + 局部态）──┐
│  容器型:  chat-log  recents-panel  machines-panel               │
│           sessions-panel  session-picker                       │
│  展示型:  chat-message  tool-card  recap-banner  session-info   │
└───────────────┬───────────────────────────────────────────────┘
                │ bare 全局调用
┌─ Layer 0 · 纯 helper（window 全局，无 DOM/state）───────────────┐
│  util.js         escapeHtml / renderMarkdown / platformIcon /  │
│                  formatRelative                                │
│  session-data.js loadSessions / saveSessions /                 │
│                  buildSessionList / defaultTitle / shortId     │
│  ＋ sidebar-resize.js（拖拽行为）  theme.js（主题，跨页共享）      │
└────────────────────────────────────────────────────────────────┘
```

`index.html` 的 `<script>` 顺序即是这个自下而上的加载：helper → 组件 →
行为 → 控制器 → app.js。

## 核心机制：`app` context bag

app.js 建一个共享对象，控制器**读它 + 把自己的公开函数挂回它**：

```js
// app.js（组装根）
const app = {
  state, api, $, TOKEN, HISTORY_PAGE_SIZE,
  curKey, botKey, uuid, setConn, showRecapBanner, closeSidebar, pickFirstSessionId,
  chatTitle, sessionInfoEl, sendBtn, chatLog, recents, machinesPanel, sessionsPanel, sessionsOf,
  refreshSessionList, fetchServerSessions,
};
ChatController(app);      // 挂 app.switchChat / sendText / addMessage / openStream / handleEvent
MachinesController(app);  // 挂 app.loadMachines / selectBot / restartMachine / restartCluster / startMachinePoll
```

```js
// *-controller.js
function ChatController(app) {
  const state = app.state;
  function switchChat(chatId) { … app.chatLog.setHistory(…) … openStream() … }
  …
  app.switchChat = switchChat;   // 回挂到 bag
}
window.ChatController = ChatController;
```

要点：

- **控制器之间通过 app bag 互通**，不直接互相 import：
  `selectBot` → `app.switchChat`（机器控制器开会话），
  `sendText` 掉线 → `app.loadMachines`（会话控制器刷机器）。
- **把依赖收进 `app` = 全部可 mock** → 控制器有真单测。这是拆出控制器的
  最大收益，不只是文件变小。曾以为 `switchChat` 是"无法测的编排胶水"，
  注入 `app` 后标题解析 / history 分页记账 / 开流全都可测。
- 控制器内部函数（addMessage、touchSession 等）保持 bare 局部调用；只有
  外部（app.js wiring / 另一个控制器）要用的才挂上 `app.`。

## 两条约定（贯穿全栈）

**1. 组件出站一律 callback，不用 CustomEvent。**
进站用注入属性 / 方法：

```js
panel.getContext = () => ({ … });    // 数据 provider（注入）
panel.render(data, ctx);             // 渲染（方法）
panel.onSelectBot = (bot, machine) => …;   // 出站意图（callback）
```

这个 app 里"组件→app.js"全是一对一，CustomEvent 的多监听 / 解耦好处用不上，
callback 更直接、少一层 `e.detail` 拆包，也跟已有的注入属性一套模式。

**2. CSS 零改动。** 容器型组件用 **light DOM** 并保留原来的 `id`
（`#messages` / `#machine-list` / `#session-list` / `#recents-list`…），host 用
`display: contents` 保住布局。所以抽组件时 `style.css` / `*.themes.css`
一行不用动。

## 一条数据流（发消息）

```
composer submit → app.sendText(text)            [chat-controller]
   → POST /api/send
   ↓ (SSE 由 switchChat 时的 openStream 已开好)
EventSource.onmessage → handleEvent(ev) 分发:
   message / stream_start / stream_delta / stream_end
        → chat-log.addMessage / el.setText  +  touchSession（刷 sessions/recents）
   tool_call / tool_result → chat-log tool card
   stream_end → fetchServerSessions → sessions-panel / recents / ctx 刷新
   _close     → setConn("offline")
```

`switchChat` 负责：关旧流 → 解析标题 → mask 遮住 → 拉首屏 history →
`chatLog.setHistory` → 开新流（`openStream`）。上滚到顶 →
`chatLog.onLoadOlder` → `loadOlderHistory` 前插分页。

## 测试（无 jsdom / 无 npm）

- `test/dom-stub.js` —— 手搓最小 DOM：`createElement` / `classList` /
  `querySelector`（tag/`[attr]`/`.class`）/ `innerHTML`（清 children）/
  事件（addEventListener·dispatchEvent）/ `requestAnimationFrame`（同步）/
  inert `EventSource` / `localStorage`。**只实现组件/控制器真正用到的子集**，
  不是通用 DOM。
- `test/load.js` —— `vm.runInThisContext` 把真实源码 eval 进 stub 全局。
- 覆盖两类：
  - **组件**：`data-in → render-out`（喂数据，断言 DOM class / 结构）。
  - **控制器**：**fake-app + spy** —— 构造假 `app`（mock `api`、spy 组件/
    switchChat），调 `app.handleEvent` / `app.switchChat` / `app.loadMachines`，
    断言对 `app` service 边界的调用（chatLog / recents / setConn / api …）。
- 接进 pytest：`tests/unit/test_web_frontend.py` shell 出 `node --test`
  （node 缺失则 skip），`uv run pytest` 一并跑。

## 加东西怎么加

- **加一个展示组件**：`components/foo.js` 里 `class Foo extends HTMLElement`，
  `connectedCallback` lazy build，`setX()/render()` 进、`onX` 回调出，
  `customElements.define` + `window.Foo`。index.html 加 `<script>`，
  test/load.js 加载 + 写 `foo.test.js`。CSS 复用旧 class（light DOM）。
- **给控制器加一个函数**：直接写在 `*-controller.js` 里用 `app.*`；只有
  app.js/别的控制器要调的才 `app.foo = foo` 挂出去。加 fake-app 测试。
- **加一个纯 helper**：`session-data.js` / `util.js` 里 `window.foo = …`，
  app.js/控制器 bare 调用（像 `escapeHtml`）。

## 文件账

| 层 | 文件 | 角色 |
|----|------|------|
| 组装根 | `app.js` | DOM refs + state + 基础 helper + app bag + 全部 wiring + boot |
| 控制器 | `chat-controller.js` | 活跃会话：流 / 事件 / 切会话 / 发送 / 历史 |
| 控制器 | `machines-controller.js` | 机器/bot 选择 + 轮询 + 重启 |
| 组件（容器）| `components/chat-log.js` | 消息滚动区（`#messages`）|
| 组件（容器）| `components/{recents,machines,sessions}-panel.js` | 侧栏三个 panel |
| 组件（容器）| `components/session-picker.js` | Claude session 恢复弹窗 |
| 组件（展示）| `components/{chat-message,tool-card,recap-banner,session-info}.js` | 气泡 / 工具卡 / recap / ctx |
| helper | `util.js` | escapeHtml / renderMarkdown / platformIcon / formatRelative |
| helper | `session-data.js` | localStorage session 读写 + server/local 合并 |
| 行为 | `sidebar-resize.js` | 侧栏拖拽改宽（自跑）|
| 主题 | `theme.js` | 主题挂载（chat + events/schedules/logs 共享）|

> **events / schedules / logs** 是三个独立小页（各自 `events.js` /
> `schedules.js` / `logs.js`），只共享 `theme.js` + `util.js`，**不走**这套
> chat 控制器架构。
