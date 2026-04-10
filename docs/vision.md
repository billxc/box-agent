# BoxAgent 愿景

## 一句话

手机打开 Telegram，就能远程操控服务器上的 AI agent。

## 核心理念

**站在巨人肩膀上**：Claude CLI 和 Codex 是大公司花大量资源打造的产品，比任何个人项目都可靠。BoxAgent 不重复造轮子，只做一件事——把这些 agent 桥接到你的手机上。

**Agent in the Box, Just for Me**：一个人、一台机器、一个 agent。不考虑多用户、不考虑权限管理、不考虑 SaaS。你的 box，你的 agent。

**5 分钟部署**：如果你已经装了 Claude CLI 或 Codex，那装 BoxAgent 就是 `pip install`、写个 config、跑起来。不需要学新框架，不需要重新配环境，直接复用你已有的一切。

## 做什么

- **消息桥接**：Telegram / Web UI / 未来可能 MS Teams 等 → AI backend → 流式回复
- **Backend 可插拔**：当前 Claude CLI + Codex，接口开放，未来可以接任何 CLI agent
- **嵌入 WebView2**：作为 WebView2 的宿主应用，提供桌面端 AI 对话界面，同时验证 WebView2 功能。**这是 BoxAgent 的差异化方向**——市面上的 AI 桥接工具都是纯 CLI 或 Web，没有人用 WebView2 做原生桌面体验
- **定时任务**：Cron 触发 AI 执行任务，结果推送到手机
- **多 Channel**：主力 Telegram（体验最好），Web UI（给没有 Telegram 的人），其他渠道按需加，但每个都要配置简单、体验好
- **媒体能力**：AI 能主动给你发图片、文件、视频

## 不做什么

- **不做 Agent 逻辑**：tool calling、RAG、记忆系统、prompt engineering——全交给 backend。跟进 agent 技术太累，让大公司去卷
- **不做多用户**：没有用户管理、没有权限系统、没有团队协作
- **不做付费/计费**：不算 token、不限额度、不出账单
- **不做重型框架**：几千行代码，你看得懂，改得动

## 体验目标

1. 用户已有 Claude CLI → `uv pip install boxagent` → 改 config → 手机开聊
2. 换 backend = 改一行配置
3. 加 channel = 改几行配置
4. 出问题能看日志自己排查，不需要翻源码
