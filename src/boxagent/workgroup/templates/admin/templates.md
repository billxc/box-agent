# Design Doc Template

Use this template when writing a spec document during the DDD flow.

```markdown
# {{Feature Name}} — Design Spec

**Date:** YYYY-MM-DD
**Author:** {{who drafted this}}
**Status:** Draft | In Review | Approved
**Stakeholders:** {{who was consulted}}

## Goal

{{One paragraph: what problem does this solve and why now?}}

## Success Criteria

- {{Measurable outcome 1}}
- {{Measurable outcome 2}}

## Proposed Approach

### Architecture

{{How the pieces fit together. Diagrams welcome.}}

### Components

| Component | Responsibility | New/Modified |
|---|---|---|
| {{name}} | {{what it does}} | New / Modified |

### Data Flow

{{How data moves through the system. Input → processing → output.}}

### Error Handling

{{What can go wrong and how we handle it.}}

### Testing Strategy

{{What gets tested, how, and what coverage looks like.}}

## Alternatives Considered

### Option A: {{name}}
{{Description and why rejected.}}

### Option B: {{name}}
{{Description and why rejected.}}

## Open Questions

- {{Anything still unresolved}}

## Out of Scope (v1)

- {{Features explicitly deferred}}
```

---

# Project Tracking Template

Use this template when onboarding to a new project.

```markdown
# {{Project Name}} — Project Tracking

**Context:** {{channel or project name}}
**Repo:** https://github.com/{{org}}/{{repo}}.git
**Product:** {{one-line description}}

## Team
- **PM:** {{name}}
- **Specialist:** {{agent name}}
- **Manager:** {{admin name}}

## Current Status
- PRD: {{status}}
- Tech Design: {{status}}
- Sprint/Week: {{current}}

## Milestones
| # | Description | Status | Branch |
|---|---|---|---|
| M1 | {{desc}} | Planned | feat/{{name}}/dev-m1 |

## Remaining Gaps
- {{item}} — {{blocker/owner}}

## Tech Stack
- Frontend: {{x}}
- Backend: {{x}}
- DB: {{x}}
- Deploy: {{x}}
```

---

# Handoff Checklist

When a specialist changes mid-project:

1. Outgoing specialist pushes all WIP to repo
2. Document current state in `memory/`
3. New specialist reads notes + repo before starting
4. Confirm new specialist can access all resources
5. Announce handoff to the human

---

# Milestone Delivery Checklist

Before marking a milestone complete:

- [ ] All tests passing
- [ ] Code committed and pushed to correct branch
- [ ] PR opened with project owner tagged for review
- [ ] Docs updated (spec status, README, tech notes)
- [ ] Memory files updated with key decisions
- [ ] Human notified of completion
