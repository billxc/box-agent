# Manual E2E test harness

Runnable scenarios that hit **real LLMs** and print per-event logs. Not
auto-asserted — read the output (or hand it to an AI) and judge whether
each backend behaved correctly.

## When to use

- Sanity check after touching backend code (event mapping, session
  resume, cancel, MCP wiring).
- Comparing backends on the same prompt (`--backend all`).
- Reproducing a bug a user reported.
- Anything that requires actual LLM cooperation — tool use, multi-turn
  recall, error recovery.

The auto-test suite (`uv run pytest`) handles everything mockable; this
harness handles everything that needs real models.

## Run

```bash
# Single backend, single scenario
uv run python -m manual_e2e.run --backend claude-cli --scenario hello

# Run all scenarios against one backend
uv run python -m manual_e2e.run --backend agent-sdk-claude --scenario all

# Run one scenario against every backend (side-by-side comparison)
uv run python -m manual_e2e.run --backend all --scenario tool_use_bash --yolo

# Pin a workspace + model
uv run python -m manual_e2e.run --backend codex-cli --scenario hello \
    --workspace /tmp/my-test --model gpt-5
```

`--yolo` auto-approves tool calls. Strongly recommended for any scenario
that involves Bash / Read — without it, backends that prompt for
permission will hang or be denied (especially `agent-sdk-copilot`,
which currently denies-all in non-yolo mode).

## Backends

| `--backend` | Auth needed |
|---|---|
| `claude-cli` | `claude` CLI installed + logged in |
| `codex-cli` | `codex` CLI installed + logged in |
| `agent-sdk-claude` | Same as `claude-cli` (SDK delegates to it) |
| `agent-sdk-copilot` | `copilot` CLI installed + logged in (or `GITHUB_TOKEN`) |
| `all` | All four — slowest, most thorough |

## Scenarios

| Name | What it exercises |
|---|---|
| `hello` | Single prompt — does any text come back at all? |
| `multi_turn_recall` | Session continuity — turn 2 must recall a word from turn 1 |
| `tool_use_bash` | Tool call event mapping — should fire `on_tool_call` / `on_tool_update` |
| `tool_use_read_file` | File-reading tool — same as above with a different tool |
| `cancel_mid_turn` | `backend.cancel()` interrupts a long generation |
| `error_recovery` | Trigger a tool error, then verify the next turn still works |

## What the log looks like

```
═══════════════════════════════════
║ claude-cli  ×  tool_use_bash    ║
═══════════════════════════════════

── Turn 1/1 ──
━━━ TURN START (claude-cli) ━━━
  prompt: Run the shell command `echo hi-from-boxagent` and tell me the output.
[+ 0.42s]  STREAM   "I'll run that for you."
[+ 0.85s]  TOOL ▶   Bash  status=in_progress
[+ 1.12s]  TOOL ✓   Bash  input={"command": "echo hi-from-boxagent"}
          result="hi-from-boxagent\n"
[+ 1.35s]  STREAM   "The output was: hi-from-boxagent"
━━━ TURN END  (1.35s, 2 chunks, 1 tool calls) ━━━
post-turn: session_id=abc-123 state=idle text_len=42 tool_calls=1

═════════════════
║ SUMMARY       ║
═════════════════

  PASS  claude-cli  tool_use_bash

Note: 'PASS' means the run completed without raising; the actual
content quality is for you (or an AI) to judge from the log above.
```

## What "PASS" means and doesn't mean

`PASS` here means **the run finished without throwing**. It says nothing
about output quality. To verify quality, read the log:

- For `hello`: did `STREAM` events contain "hello world"?
- For `multi_turn_recall`: did turn 2's stream mention "pineapple"?
- For `tool_use_bash`: did a `TOOL ✓ Bash` line appear with the right command?
- For `cancel_mid_turn`: did the turn end before counting to 100?
- For `error_recovery`: did turn 2 say "OK, recovered" despite turn 1 erroring?

## Adding a scenario

Edit `manual_e2e/scenarios.py`. Add a function returning a list of
`(prompt, post_action)` tuples, then register it in `SCENARIOS`. See
`cancel_mid_turn` for an example using `post_action` to interrupt mid-turn.

## When to add an auto-test instead

If a behaviour can be validated without a live LLM (event ordering,
state transitions, factory wiring, validator rejection, etc.), use the
mock-based unit tests under `tests/unit/test_*_protocol.py` /
`test_*_process.py`. Reserve manual E2E for things only an LLM can
demonstrate.
