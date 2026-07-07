# 开发流程与约定

> 动手写代码前读这页。规范源头是仓库根 `CLAUDE.md`（checked-in，最新），本页浓缩 + 补充
> 代码核对的事实。

## 迭代工作流（每步做完确认再往下）

1. **明确任务** —— bug 还是 feature？范围不确定就问，不要脑补。
2. **调研（最重要）** —— `grep -n` 找全相关路径，从入口追调用链到末端，读相关测试。**追完整条链**（Claude 和 Codex 两条线都看），结论要带具体 `file:line + 函数名`。
3. **写测试（先红灯）** —— 在 `tests/unit/test_*.py` 写，覆盖 happy path + 边界 + 错误/fallback + 相邻交互。确认新测试 fail、旧测试全绿。
4. **实现** —— 改动尽量小；不顺手重构不相关代码；不加"以后可能用到"的抽象。
5. **验证** —— `uv run pytest -x -q`，新旧全绿，测试数不降。
6. **更新文档（同一 commit）** —— 改了行为更新代码库文档；新决策追加 `docs/decisions.md`；本 openwiki 相关页也一并更（跑 `/openwiki update`）。
7. **提交** —— 小步提交，前缀 `fix:`/`feat:`/`refactor:`/`docs:`/`tests:`（bug 用 `fix(BUGxxx):`）。⚠️ **禁止 `git add -A`/`git add .`**，只 `git add <明确路径>`；commit message 不写 AI 自己。
8. **汇报** —— 改了什么（文件+行）、测试结果、commit hash。

变体：纯调研（只 Step 1-2，不动代码）；快修（Step 3+5 不能省）；大功能（拆多个独立可测的 commit）。

## 测试约定

- 单测 `tests/unit/test_*.py`，集成 `tests/integration/`（`pyproject.toml` 里 `-m 'not integration'` 默认 skip）。
- **当前基线：`uv run pytest --collect-only` 收 ~977 个（7 个 integration deselected）。只能涨不能降。**（CLAUDE.md 里写的 886 已过时——以实际 collect 为准。）
- **Backend / Channel 用 `boxagent.testing.MockBackend` / `MockChannel`，别手搓 AsyncMock**（`testing/mocks.py`）：
  ```python
  from boxagent.testing.mocks import MockBackend, MockChannel
  backend = MockBackend(bot_name="test"); backend.start()
  backend.script(["chunk1", "chunk2"])       # 脚本化 stream chunks
  backend.script_handler(custom_async_fn)     # 复杂行为：raise / 同步 event
  backend.fail_next_turn("error msg")         # 模拟 turn 失败（last_turn_failed=True）
  channel = MockChannel()
  assert backend.sends[-1].message == "..."   # SendCall 记录
  assert channel.sent_texts[-1] == ("chat_id", "...")
  ```
- **黑盒 e2e 范本**：`tests/unit/test_router_e2e.py` —— 只断言 `MockBackend.sends` + `MockChannel.sent_texts/streams`，**从不 peek** Router 私有状态（`_compact_summaries` 之类）。新写整链路测试照这个模板。
- 前端有自己的测试：`src/boxagent/transports/web/static/test/*.test.js`（`node --test` + 自写 DOM stub，无 jsdom）。

## 命名规范（硬性，不许缩写）

**禁止缩写命名变量/参数/函数/属性**，一律完整英文单词。血泪教训：`mid`（在 cluster 是
machine_id，在 transports 是 message_id —— 同缩写两义）、`sess`/`proc`/`mgr`/`cfg`/`caps`/`opts`/`inst`
等一律展开成 `session`/`process`/`manager`/`config`/`capabilities`/`options`/`install_parser`。

例外仅限：Python 习语（`i`/`e`/`f`/`args`/`kwargs`/`self`/`cls`、typing 的 `T`/`K`/`V`）、
第三方 API 关键字（argparse `dest=`）、协议缩写（`mcp`/`rpc`/`http`）、项目核心域词（`bot`）。

工具：`scripts/naming_audit.py` 跑一下看当前 suspect。代码注释用**中文**、言简意赅。

## 已知坑（核对代码后仍然成立的，别重蹈）

1. **`claude-cli` 已静默重定向到 `agent-sdk-claude`**（`backend_factory.py:54`）。测试里别 patch `ClaudeProcess`（**文件已删除**），patch `AgentSDKClaude`。
2. **watchdog / scheduler / router 持旧 backend 引用**：`restart_bot` / `on_backend_switched` 换 backend 时**三处引用一起更**（`agent_manager.py:394-413`），漏一个就用到死 backend。
3. **`/compact` 跨 compact 丢老 session（BUG88/89 已修）**：靠 storage 链式保存（`sessions.yaml` 的 `previous_session_ids`）+ raw-read jsonl。再动 compact 流程跑 `tests/unit/test_session_chain*.py`。
4. **`_compact_summaries` / `_resume_contexts` 是 Router 内存 dict**（`router/core.py:44`），跨进程重启丢。只在 turn 成功后消费（失败留着重试）。
5. **Codex 事件不能用 `create_task`**（会乱序），已改 `await`，别改回去。
6. **Codex session 不能跨重启恢复**：别把 `thread_id` 当 Claude 式恢复凭据。
7. **`mcp-port.txt` 被外部清掉 → codex-cli 静默无 MCP**（`mcp/server.py` gate）。重启 boxagent 重写。SDK 后端走 in-process MCP 不受影响。
8. **devtunnel 跨 region 同名 tunnel**：必须 `devtunnel list -j` + bare-name 过滤，>1 个 warn + 选 active，**不要自动 delete**（删错 region 关掉自己）。
9. **HostElection 提升前不 retry probe → split-brain**：promote 前 retry probe 一轮（`promote_retry_count=3`），probe 异常用 `repr(exc)` 记类型。
10. **cluster 中继 sender 无验证**：host 信任任何标 `trusted=True` 的中继消息来源（`transports/base.py:33`）。信任模型软点，跨机 auth 时记着。
11. **业务代码禁止直接 import `boxagent.events`**：写事件走 `boxagent.log` facade（`get_logger`/`log.<level>`）。

## 既有边界别乱动

模块边界（router / agent / transports / cluster / events / bus）是反复重构后的产物，**不要"顺手"动**；
Core 不 import cluster；bus 是中立 leaf。稳定性优先，能 30 行解决的不写 class。

## 需求 / Issue 跟踪：yait

本仓库用 `yait` 管需求/调研，项目名固定 `box-agent`：

```bash
yait -P box-agent list                       # 列 issue
yait -P box-agent show <ID>                   # 详情
yait -P box-agent new "标题" -t feature -p p2 --body "正文"   # type∈{feature,bug,enhancement,misc}
yait -P box-agent comment <ID> "进展"
yait -P box-agent close <ID>
```

约定：每次接需求 / 做完调研先开 issue 留痕（调研类标题加 `调研：`）。大需求拆主+子 issue 用
`blocks`/`depends-on` 串。

## 快速命令

```bash
uv run pytest -x -q                                   # 全量测试
uv run pytest tests/unit/test_router_e2e.py -x -q      # 单文件
uv run boxagent --config ~/.boxagent/config.yaml       # 启动
uv run boxagent doctor --fix                            # 依赖体检
git log --oneline -5 && git status -sb
```

> 包管理用 `uv`（`uv run --with <pkg>` 临时装包），**不要 `pip install --break-system-packages`**。
