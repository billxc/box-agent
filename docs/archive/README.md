# Archived design docs

Frozen historical records — design proposals or decision drafts that were
either fully implemented (so the code is now the source of truth) or
explicitly not taken. Kept for context, not as instructions.

| File | Status | Why archived |
|---|---|---|
| `agent-env-design.md` | implemented | `AgentEnv` shipped; see `src/boxagent/agent_env.py` and `current-architecture.md` |
| `raw-bot.md` | implemented | `BotConfig.passthrough` + `RawSessionPool` shipped |
| `remove-static-specialists-design.md` | implemented | `_builtin_specialists` removed; templates shipped (simplified vs proposal) |
| `template-system-open-questions.md` | implemented | All Q1-Q11 resolved; `template_loader.py` + `templates/builtin_templates/` |
| `workgroup-design.md` | superseded | Discord-centric framing invalidated by 2026-05-08 Discord removal; current workgroup docs in `codebase-guide.md` + `current-architecture.md` |
| `workgroup-role-design-analysis.md` | not taken | Planner role proposal not implemented; `SpecialistConfig.role` does not exist |

If you need to **revive** any of these proposals, do not modify the
archived file in place — extract the still-relevant parts into a new doc
under `docs/` and link back.
