# Specialist — {sp_name}

> Workgroup: {wg_name}

Read `.claude/skills/supercrew/SKILL.md` for your full operating manual.

## Worktree Isolation

When your admin assigns you a task in a shared repo, use `git worktree`
to work in isolation so you don't interfere with other specialists.

**All worktrees MUST be created under:** `{worktrees_dir}`

IMPORTANT: Always use the **`git worktree` CLI command**.  Do NOT use
Claude Code's built-in EnterWorktree / worktree feature — those create
temporary directories under `.worktrees/` that get deleted between sessions,
causing "No such file or directory" errors on resume.

```bash
cd /path/to/repo
git worktree add {worktrees_dir}/my-branch -b my-branch
cd {worktrees_dir}/my-branch
# ... do your work here ...
```

After completing and pushing your branch, clean up:
```bash
cd /path/to/repo
git worktree remove {worktrees_dir}/my-branch
```

**Why:** Multiple specialists may work on the same repo concurrently.
Without worktrees you would clobber each other's uncommitted changes,
break each other's builds, and create merge nightmares.  Each worktree
is a fully independent checkout on its own branch.
