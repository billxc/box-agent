# Naming Rename Proposal

Companion to the naming audit produced by `scripts/naming_audit.py`.
Tracks yait #15 / #16 / #68.

> The raw audit snapshot lives in yait #16 (and as a local-only
> `docs/naming-audit.md` after `--write`, gitignored). This file is the
> human-curated rename plan. Re-run the script to refresh the raw audit;
> this proposal is hand-checked.

## How this list was produced

1. `scripts/naming_audit.py` walks `src/boxagent/` AST + filenames, flags
   identifiers that are either single-token short (≤3 chars) or contain
   a known offender token (`sat`, `dt`, `dc`, `wapp`, `tname`, `wh`,
   `mgr`, `syn`, `ch`, `st`, `cb`, `cid`, …). Output: `naming-audit.md`.
2. `scripts/naming_ambiguity_check.py <name>...` opens every def site
   and checks whether the variable means the same thing across uses.
   Used to verify a mechanical rename won't merge two unrelated concepts.

## Abbreviation table (verified by ambiguity check)

Sorted by usage frequency. ✓ = ambiguity-checked, all sites share the
same logical meaning. ✗ = different meanings — DO NOT mass-rename.

| 缩写 | 含义 | 建议改成 | refs / defs | check |
|---|---|---|---:|:---:|
| `ch` | channel (Telegram / Discord / Web 都用过；都是 channel 抽象) | `channel`（多数处） | 78 / 20 | ✓ |
| `mid` | message_id（Telegram/Discord stream message id） | `message_id` | 76 / 15 | ✓ |
| `bot` | bot | （不改，已是全称） | 60 / 20 | ✓ |
| `st` | `_ChatState`（仅 raw_pool.py） | `state` | 54 / 15 | ✓ |
| `cfg` | BotConfig / WorkgroupConfig | `config` | 35 / 10 | ✓ |
| `wg` | WorkgroupConfig | `workgroup`（局部）/ `workgroup_config` | 35 / 10 | ✓ |
| `sp` | specialist (SpecialistConfig) | `specialist` | 28 / 4 | ✓ |
| `dc_channel` | discord channel object | `discord_channel` | 23 / 6 | ✓ |
| `syn_cfg` | synthesized BotConfig（specialist 用） | `synth_bot_config` | 20 / 1 | ✓ |
| `cli` | BaseCLIProcess instance | `cli_process` | 18 / 7 | ✓ |
| `wapp` | aiohttp.Application (web UI app) | `web_app` | 16 / 1 | ✓ |
| `web_ch` | WebChannel | `web_channel` | 14 / 4 | ✓ |
| `wh` | discord webhook | `webhook` | 14 / 4 | ✓ |
| `cat` | discord category | `category` | 14 / 3 | ✓ |
| `cb` | AgentCallback | `callback` | 7 / 2 | ✓ |
| `cid` | chat_id（discord 那处是 int channel id；其它是 str） | `chat_id` | 6 / 3 | ✓ |
| `tname` | tunnel_name | `tunnel_name` | 5 / 1 | ✓ |
| `hb` | HeartbeatManager | `heartbeat` | 5 / 2 | ✓ |
| `dc_user_id` | discord_user_id | `discord_user_id` | 4 / 1 | ✓ |
| `dt_token` | devtunnel JWT | `devtunnel_token` | 2 / 1 | ✓ |
| `_workgroup_mgr` | WorkgroupManager (gateway field) | `_workgroup_manager` | 1 / 1 | — |
| `mgr` (params) | WorkgroupManager | `manager` | 多处 | — |
| `_sat_client` | GuestClient (gateway field) | `_guest_client` | 1 / 1 | ✅ |
| `_sat_registry` | GuestRegistry (gateway field) | `_guest_registry` | 1 / 1 | ✅ |
| `sat_client.py` | guest_client (module) | `guest_client.py` | — | ✅ |
| `/api/sat/ws` (wire) | guest WS endpoint | `/api/guest/ws`（保留旧路径 alias 一段时间） | — | ✅ |

### Found-but-NOT-renaming (验证后剔除)

| 缩写 | 实际含义 | 为什么留 |
|---|---|---|
| `dt` | `datetime.fromisoformat(...)` 局部变量 (`sessions/cli.py:279`) | 不是 devtunnel 缩写。1 处使用，scope 局部，留着 |
| `t0` / `t1` / `t2` | ContextVar token / `time.time()` | 局部、惯例，无歧义 |
| `_run` | 函数名 `async def _run` | 模块内私有方法名，惯例 |
| `__all__` | Python 模块导出列表 | 语言惯例 |
| `_bot` / `_dp` | aiogram 框架字段名 | 框架惯例 |
| `add` / `dl` / `en` / `dis` / `run` / `ls` | scheduler/cli.py 子命令处理函数 | 内部一致命名风格，scope 局部 |
| `bl` / `pl` / `cl` / `blk` | sessions/cli.py / claude_native.py 局部 block/payload | scope 极小 |
| `proc`, `mcp`, `rpc`, `app`, `cwd`, `arg`, `raw`, `idx`, `cmd`, `npm`, `txt` | 通用缩写 | 全行业惯例 |
| `via`, `gap`, `gw`, `wd`, `req`, `ev`, `dm`, `sig`, `rc`, `rem`, `fh`, `exc`, `att` | 局部短变量 | 用量低 + 局部清晰 |

## How to use this table

For each row in the abbreviation table, mark one of:
- ✅ approve — execute the rename
- ⏭ skip — keep as-is
- 🔧 amend — change the proposed new name to something else

Owner edits this file; the renamer reads `✅` rows and executes mechanical
find-and-replace, batched per cluster (e.g. all `dc_*` together, all
`sat_*` together). pytest must stay green between batches.

## Decisions log

| Date | Old → New | Commit |
|------|-----------|--------|
| 2026-05-03 | `syn_cfg` → `bot_config` (workgroup/manager.py, 20 refs) | 1ba97bb |
| 2026-05-03 | `wapp` → `web_app` (gateway.py, 16 refs + 1 NOTE) | 1ba97bb |
| 2026-05-03 | `dt_token` → `devtunnel_token` (cluster/sat_client.py, 2 refs) | 1ba97bb |
| 2026-05-04 | `cli` → `cli_process` (gateway.py + workgroup/manager.py, 12 sites) | 46e19cd |
| 2026-05-04 | `dc_channel` → `discord_channel` (gateway.py + manager.py + mcp_http.py, 21 sites) | 46e19cd |
| 2026-05-04 | `dc_user_id` → `discord_user_id` (gateway.py, 4 sites) | 46e19cd |
| 2026-05-04 | `tname` → `effective_tunnel_name` (cluster/sat_client.py, 5 sites) | 46e19cd |
| 2026-05-04 | `web_ch` → `web_channel` (gateway.py + workgroup/manager.py, 11 sites) | (this batch) |
| 2026-05-04 | `hb` → `heartbeat` (workgroup/manager.py, 5 sites) | (this batch) |
| 2026-05-04 | `ch_id` → `channel_id` (mcp_http.py, 3 sites — was discord channel id, not chat_id; corrected from earlier proposal) | (this batch) |
| 2026-05-04 | `cb` → `callback` (acp_process.py, 8 sites) | (this batch) |
| 2026-05-04 | `ref` → `bot_ref` (scheduler/engine.py, 19 sites incl. docstring) | (this batch) |
| 2026-05-04 | `cat` → `category` (discord.py + config.py, 14 sites) | 43895bd |
| 2026-05-04 | `sp_*` → `specialist_*` (workgroup/manager + mcp_http + 4 tests, 340 sites; +template `{sp_name}` placeholder; dropped `import subprocess as sp` alias) | ff0e209 |
| 2026-05-04 | `wg_*` → `workgroup_*` (config + gateway + manager + 5 tests, 570 sites; +template `{wg_name}` placeholder; preserved wire `wg:` chat_id and `/api/wg/peer/recv`) | b3db003 |
| 2026-05-04 | `cid` split (channel_id in discord.py, chat_id in gateway/heartbeat, 5 sites) | (this batch) |
| 2026-05-06 | `_sat_*` → `_guest_*` (gateway field + 50+ refs) + `sat_client.py` → `guest_client.py` + classes `Satellite{Client,Registry,Session}` → `Guest{Client,Registry,Session}` + config `satellite_token` → `guest_token` (internal field; yaml `cluster.token` unchanged) + wire `/api/guest/ws` (legacy `/api/sat/ws` alias kept) — closes #68 | (this batch) |
