# 决策日志

记录每次偏离原始设计的决定，以及原因。

---

## 2026-03-22: 删除 max_workers 和 display.streaming

**决定**: 从 BotConfig 中删除 `max_workers` 和 `display_streaming` 字段。

**原因**: BoxAgent 定位是轻量桥接，不做并发管理（backend 自己处理），所以 worker pool 不会实现。`display.streaming` 虽然能解析但运行时没有消费，Telegram 始终流式输出。与其留着死代码误导人，不如删掉，需要时再加。

---

## 2026-03-22: 文档归档

**决定**: 将所有早期设计文档移入 `docs/archive/`，只保留反映当前实现的文档。

**原因**: 项目从设计到实现过程中大幅收敛，早期文档（V1 设计、V2 路线图、实现计划）与实际代码不一致，容易误导维护者。`codebase-guide.md` 已经准确描述了当前状态。

**归档的文件**:
- `2026-03-20-boxagent-design/` — 初版英文设计
- `2026-03-20-boxagent-design.zh-CN/` — 初版中文设计
- `plans/2026-03-20-boxagent-v1/` — V1 实现计划（10 个文件）
- `2026-03-20-boxagent-v1-implementation.md` — V1 实现回顾
- `2026-03-21-boxagent-v2-design.md` — V2 设计 + 路线图
- `2026-03-22-codex-recovery-fix.md` — Codex 恢复修复分析
- `boxagent-vision-vs-current.drawio` — 愿景 vs 现状对比图

**保留的文档**:
- `README.md` — 入口
- `codebase-guide.md` — 代码库导读（现状文档）
- `decisions.md` — 本文件

---

## 2026-03-22: 需求收敛记录

以下是原始设计中提出但未实现的功能，以及当前判断：

| 功能 | 原始设计 | 当前状态 | 判断 |
|------|----------|----------|------|
| Web UI Channel | V1 设计 | 未实现 | 冻结 — 需求不明确 |
| Git 同步管理 (SyncManager) | V1 设计 | 未实现 | 冻结 — 单机够用 |
| LiteLLM / API Backend | V1 设计 | 未实现 | 冻结 — claude-cli + codex-acp 够用 |
| 自定义 Python Backend | V1 设计 | 未实现 | 冻结 |
| 知识库与偏好系统 | V1 设计 | 未实现 | 冻结 |
| CLIProcessPool (多 worker) | V1 设计 | 未实现 | 冻结 — isolate scheduler 暂够用 |
| Rate Limiting | V2 路线图 | 未实现 | 想要 — 防跑飞 |
| Conversation Logging | V2 路线图 | ✅ 已实现 | JSONL per session |
| Skill Registry | V2 路线图 | 未实现 | 冻结 — symlink 够用 |
| Structured JSON Logging | V2 路线图 | 已实现 | ✅ |
| Scheduler | V2 路线图 | 已实现 | ✅ |
| display.streaming 配置 | 配置已解析 | ✅ 已删除 | 2026-03-22 清理 |
| PID 跟踪 | 辅助代码存在 | ✅ 已删除 | 2026-03-22 清理 |
| max_workers 配置 | 配置已解析 | ✅ 已删除 | 2026-03-22 清理 |

---

## 2026-03-22: 文档与代码对齐

**决定**: 全面审查 usage-guide、codebase-guide、status、decisions，清理已删除功能的残留引用，补充新实现的 transcript 功能。

**变更**:
- usage-guide: 删除 `display.streaming` 配置说明和示例、PID 目录引用，新增 transcripts 目录
- codebase-guide: 删除 PID 跟踪章节和 display.streaming 章节，新增 Transcript 章节，更新死代码章节
- status: 重写已知 bug 部分（区分已修复/未修复），更新功能对照表
- decisions: 更新需求收敛表格中 4 项的状态

**原因**: 项目快速迭代 3 天，一天内删了 3 类死代码 + 加了 transcript，文档和代码已严重不一致。Owner 需要一个可信的文档作为理解代码的入口。

---

## 2026-03-22: 愿景新增 WebView2 集成

**决定**: 在愿景中加入 WebView2 集成方向。

**原因**: BoxAgent 可以兼做 WebView2 宿主应用——既提供桌面端 AI 对话界面（Web UI channel 的一种实现），又能验证 WebView2 功能，一石二鸟。
