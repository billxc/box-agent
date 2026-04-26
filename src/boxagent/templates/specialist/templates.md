# Development Workflow Templates

## Milestone Tracking

Use this template when planning or tracking multi-milestone development.

```markdown
# {{Project Name}} — Milestone Tracker

**Repo:** {{repo-url}}
**Base Branch:** {{main|dev}}
**Start Date:** YYYY-MM-DD

## Milestones

| # | Description | Status | Branch | PR | Notes |
|---|---|---|---|---|---|
| M1 | {{description}} | Planned | feat/{{feature}}/dev-m1 | — | — |
| M2 | {{description}} | Planned | feat/{{feature}}/dev-m2 | — | — |
| M3 | {{description}} | Planned | feat/{{feature}}/dev-m3 | — | — |

**Status key:** Planned · In Progress · Complete · Blocked

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| YYYY-MM-DD | {{what was decided}} | {{why}} |

## Blockers

| Date | Blocker | Owner | Status |
|---|---|---|---|
| YYYY-MM-DD | {{description}} | {{who can unblock}} | Open/Resolved |

## Lessons Learned

- {{lesson from this project}}
```

---

## Daily Log

Use this template for daily development notes.

```markdown
# YYYY-MM-DD — Daily Log

## What I Did
- {{task/milestone}}: {{what was accomplished}}

## Decisions Made
- {{decision}}: {{rationale}}

## Blockers
- {{blocker}}: {{status, who to ask}}

## Tests
- Added: {{number}} tests for {{component/feature}}
- Passing: {{number}}/{{total}}
- Failing: {{list any failures and why}}

## Tomorrow
- [ ] {{next task}}

## Notes
- {{anything worth remembering}}
```

---

## PR Checklist

Use this checklist before opening or merging a PR.

```markdown
## PR: {{title}}

**Branch:** `{{branch-name}}` → `{{base-branch}}`
**Issue:** #{{issue-number}} (if applicable)

### Pre-Submit
- [ ] Code compiles/builds without errors
- [ ] All existing tests pass
- [ ] New tests added for new functionality
- [ ] Edge cases and error paths tested
- [ ] No hardcoded secrets, API keys, or credentials
- [ ] No personal names, internal URLs, or private data
- [ ] Documentation updated (README, API docs, usage guides)
- [ ] Self-reviewed the full diff
- [ ] Commit messages are clear and follow convention
- [ ] Branch is up to date with base branch

### Code Quality
- [ ] Functions are small and focused
- [ ] Error handling is proper (no silent failures)
- [ ] No dead code or commented-out blocks
- [ ] Follows project's existing patterns and conventions
- [ ] No unnecessary dependencies added

### Deployment (if applicable)
- [ ] Database migrations are reversible
- [ ] Environment variables documented
- [ ] Docker build tested locally
- [ ] CI pipeline passes
- [ ] Deployment guide updated

### Post-Merge
- [ ] Verify CI/CD pipeline completes
- [ ] Confirm deployment (if auto-deploy)
- [ ] Close related issues
- [ ] Update milestone tracker
```

---

## Bug Investigation

Use this template when investigating and fixing bugs.

```markdown
# Bug: {{short description}}

**Reported:** YYYY-MM-DD
**Severity:** critical|high|medium|low
**Component:** {{affected component/module}}
**Issue:** #{{issue-number}} (if applicable)

## Symptoms
- {{what the user/system observes}}
- {{error messages, logs, screenshots}}

## Reproduction Steps
1. {{step}}
2. {{step}}
3. {{expected vs actual result}}

## Investigation

### Hypothesis 1: {{description}}
- Evidence for: {{what supports this}}
- Evidence against: {{what contradicts this}}
- Verdict: confirmed|rejected|needs more data

### Hypothesis 2: {{description}}
- Evidence for: {{what supports this}}
- Evidence against: {{what contradicts this}}
- Verdict: confirmed|rejected|needs more data

## Root Cause
{{what actually caused the bug}}

## Fix
- **Branch:** `fix-{{bug-description}}`
- **Changes:** {{summary of what was changed}}
- **Test:** {{how the fix was verified}}
  - [ ] Wrote failing test reproducing the bug
  - [ ] Test passes after fix
  - [ ] No regressions in related tests

## Lessons Learned
- {{what to do differently to prevent similar bugs}}
```

---

## Handoff / Onboarding

Use this template when handing off a project to another developer or
onboarding to an existing project.

```markdown
# Project Handoff: {{Project Name}}

**Date:** YYYY-MM-DD
**From:** {{outgoing specialist}}
**To:** {{incoming specialist}}

## Project Overview
- **Repo:** {{repo-url}}
- **Description:** {{one-line summary}}
- **Tech Stack:** {{languages, frameworks, databases, infra}}
- **CI/CD:** {{pipeline description}}

## Current State
- **Active Branch:** {{branch name}}
- **Current Milestone:** M{{n}} — {{description}}
- **Milestone Status:** {{planned|in-progress|blocked}}
- **Open PRs:** {{list with links}}
- **Open Issues:** {{list with links}}

## Architecture
- {{high-level overview of the codebase structure}}
- {{key modules and their responsibilities}}
- {{data flow summary}}

## Development Setup
1. Clone: `git clone {{repo-url}}`
2. Install dependencies: `{{command}}`
3. Configure environment: `cp .env.example .env` and fill in values
4. Run tests: `{{command}}`
5. Start dev server: `{{command}}`

## Key Files
| File/Directory | Purpose |
|---|---|
| {{path}} | {{what it does}} |

## Known Issues / Tech Debt
- {{issue}}: {{description and impact}}

## Decisions History
| Date | Decision | Rationale |
|---|---|---|
| YYYY-MM-DD | {{decision}} | {{why}} |

## Checklist
- [ ] Incoming specialist has repo access
- [ ] Incoming specialist can build and run locally
- [ ] Incoming specialist has reviewed this document
- [ ] All WIP pushed to repo
- [ ] Memory files updated
- [ ] Handoff announced to admin
```
