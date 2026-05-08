# BoxAgent 愿景

## 一句话

把分布在多台机器上的多个 AI agent 拧成一个能从手机/浏览器协同操控的网络。

## 核心理念

**Personal Agent Network**：一个人，多台机器，多个 agent，协同交互。
- 一个人 — 单用户，无多租户/权限/SaaS
- 多机器 — 你的笔记本、台式、远端 dev box 都能加入同一张网
- 多 agent — 一台机器可以跑多个 agent，扮演不同角色
- 协同交互 — agent 之间可以互相派活、互相通知

**站在巨人肩膀上**：Claude CLI 和 Codex 是大公司花大量资源打造的产品，比任何个人项目都可靠。BoxAgent 不重复造 agent，只做编排和桥接。

**Core + 扩展的分层**：
- **Core** — 一台机器、一个 agent、若干 channel（Telegram / Web / iOS / MCP）就能跑。pip install + 写 config 即用。
- **Cluster 扩展** — 多机加入同一张网（hub-and-spoke + devtunnel）。
- **Workgroup 扩展** — 同机/跨机的 admin/specialist 协同。

Core 不依赖任何扩展。两个扩展可独立启用。

## 做什么

- **消息桥接**：Telegram / Web UI / iOS native / MCP / 未来其他 → AI backend → 流式回复
- **Backend 可插拔**：当前 Claude CLI、Codex CLI、Codex ACP；接口开放，加新 CLI agent = 写一个 adapter
- **多机协同**：节点之间通过 devtunnel 自动建网，host 上的 web UI 能看到全网 bot 并直接对话
- **Agent 协同**：admin agent 可以把任务派给 specialist；admin 之间也能互发消息
- **定时任务**：Cron 触发 agent 执行任务，结果推送到手机
- **媒体能力**：AI 能主动发图片、文件、视频

## 不做什么

- **不做 Agent 逻辑**：tool calling、RAG、记忆系统、prompt engineering 全交给 backend
- **不做多用户**：没有用户管理、没有权限系统、没有团队协作（同一个用户在多机/多 agent 间穿梭，不是多人）
- **不做付费/计费**
- **不做重型框架**：core 几千行代码，扩展每个也是几千行，看得懂、改得动

## 体验目标

1. 用户已有 Claude CLI → `uv pip install boxagent` → 改 config → 手机开聊
2. 换 backend = 改一行配置
3. 加 channel = 改几行配置
4. 多机加入网络 = 在 config 里写一行 cluster 配置
5. 加 specialist = 在 config 里写一段 workgroup
6. 出问题能看日志自己排查，不需要翻源码
