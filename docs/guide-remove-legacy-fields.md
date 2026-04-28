# Guide: Remove Legacy Fields from base_cli

## Goal

Delete `bot_token`, `is_workgroup_admin` from `base_cli.py` (and `bot_token` from `acp_process.py`). All callers should use `AgentEnv` instead.

## Fields to Remove

| File | Field | Used by |
|------|-------|---------|
| `agent/base_cli.py:40` | `bot_token: str = ""` | claude_process, codex_process (fallback when env=None) |
| `agent/base_cli.py:42` | `is_workgroup_admin: bool = False` | claude_process (fallback when env=None), workgroup/manager.py (sets it) |
| `agent/acp_process.py:219` | `bot_token: str = ""` | acp_process (fallback when env=None) |

## Callers That Still Don't Pass env

### 1. `router/core.py:494` — compact command
```python
await proc.send(summary_prompt, collector)  # no env
```
**Fix:** Build env from `self._build_env(msg)` — but `msg` is available in the calling method `_cmd_compact`. Thread it through.

### 2. `router/core.py:420-439` — /backend switch command
```python
bot_token = getattr(old_proc, "bot_token", "")
...
new_proc = ClaudeProcess(bot_token=bot_token, ...)
```
**Fix:** Don't pass `bot_token` to new process. The router's `_build_env` already reads `telegram_token` from `getattr(self.cli_process, "bot_token", "")`. But after removing the field, this breaks. Instead, store telegram_token on the Router itself (it comes from BotConfig at startup and doesn't change).

### 3. `gateway.py:59,70,79` — _create_backend
```python
ClaudeProcess(bot_token=bot_cfg.telegram_token, ...)
CodexProcess(bot_token=bot_cfg.telegram_token, ...)
ACPProcess(bot_token=bot_cfg.telegram_token, ...)
```
**Fix:** Stop passing `bot_token`. These processes will always receive env from Router when called via dispatch. The token is in `AgentEnv.telegram_token`.

### 4. `workgroup/manager.py:214,220` — sets is_workgroup_admin on process
```python
admin_cli.is_workgroup_admin = True
proc.is_workgroup_admin = True  # in _admin_factory
```
**Fix:** Don't set it on process. Router's `_build_env` already sets `workgroup_role="admin"` when `getattr(self.cli_process, "is_workgroup_admin", False)` is True. Need to change this to read from Router directly: add `self.workgroup_role` field to Router, set in workgroup/manager.py when creating admin router.

## Step-by-Step

### Step 1: Add `telegram_token` and `workgroup_role` to Router
```python
# router/core.py — add fields
telegram_token: str = ""      # from BotConfig at startup
workgroup_role: str = ""      # "admin" / "specialist" / ""
```

### Step 2: Set them in gateway and workgroup
```python
# gateway.py — _start_bot
router = Router(..., telegram_token=bot_cfg.telegram_token)

# workgroup/manager.py — admin router
admin_router = Router(..., workgroup_role="admin")
# specialist router (optional, specialists don't need it)
```

### Step 3: Update _build_env to read from Router instead of process
```python
# router/core.py — _build_env
telegram_token=self.telegram_token,  # was: getattr(self.cli_process, "bot_token", "")
workgroup_role=self.workgroup_role,  # was: "admin" if getattr(self.cli_process, "is_workgroup_admin", False) else ""
```

### Step 4: Remove env=None fallback in all three backends

**claude_process.py** — delete the `else:` branch:
```python
# Before:
if env is not None:
    mcp_bot_name = env.bot_name
    mcp_is_admin = env.is_workgroup_admin
    mcp_telegram_token = env.telegram_token
    ...
else:
    mcp_bot_name = self.bot_name     # DELETE
    mcp_is_admin = self.is_workgroup_admin  # DELETE
    mcp_telegram_token = self.bot_token     # DELETE
    ...

# After: just use env (error if None — should never happen now)
mcp_bot_name = env.bot_name if env else self.bot_name
mcp_is_admin = env.is_workgroup_admin if env else False
mcp_telegram_token = env.telegram_token if env else ""
```

Actually simpler — since all callers now pass env, just assert:
```python
assert env is not None, "AgentEnv required"
mcp_bot_name = env.bot_name
...
```

But for safety, keep a minimal fallback using `self.bot_name` (which stays on base_cli) and empty strings for token/admin.

**codex_process.py:**
```python
# _mcp_args: token = env.telegram_token if env else ""
# _extra_env: same
```

**acp_process.py:**
```python
# _ensure_connected: token = env.telegram_token if env else ""
```

### Step 5: Remove bot_token from _create_backend
```python
# gateway.py — stop passing bot_token
ClaudeProcess(workspace=..., model=..., yolo=..., bot_name=...)  # no bot_token
CodexProcess(workspace=..., model=..., yolo=..., bot_name=...)   # no bot_token
ACPProcess(workspace=..., model=...)                              # no bot_token
```

### Step 6: Remove workgroup_admin from workgroup manager
```python
# workgroup/manager.py — don't set on process
# admin_cli.is_workgroup_admin = True  ← DELETE
# proc.is_workgroup_admin = True        ← DELETE
```

### Step 7: Fix compact command to pass env
```python
# router/core.py _cmd_compact — build env from the original msg
env = self._build_env(msg)
await proc.send(summary_prompt, collector, env=env)
```

### Step 8: Fix /backend switch
```python
# router/core.py _cmd_backend — don't copy bot_token from old process
# Remove: bot_token = getattr(old_proc, "bot_token", "")
# Remove: bot_token=bot_token from all three process constructors
```

### Step 9: Delete the fields
```python
# agent/base_cli.py — delete lines 40 and 42:
# bot_token: str = ""
# is_workgroup_admin: bool = False

# agent/acp_process.py — delete line 219:
# bot_token: str = ""
```

### Step 10: Fix tests
Search for `bot_token=` and `is_workgroup_admin=` in tests/ and remove or update.

## Verification
```bash
python3 -c "import ast, glob; [ast.parse(open(f).read()) for f in glob.glob('src/boxagent/**/*.py', recursive=True)]; print('OK')"
uv run --with pytest --with pytest-asyncio pytest tests/unit/ -q --deselect tests/unit/test_commands.py::TestResumeCommand
```
