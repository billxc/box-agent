# Builtin Specialist Templates

This directory ships with the codebase. Drop a template here to make it
available to all workgroups.

A template is a directory with at minimum:
- `description.md` — one-line description shown in `list_templates`
- `CLAUDE.md` — prompt fragment appended to the system layer

Optional:
- `skills/` — each subdir is a skill, symlinked into the specialist
- `extra_skill_dirs.txt` — external skill parent dirs (paths relative to `~/.boxagent/`)
- `extra_skill_allows.txt` / `extra_skill_blocks.txt` — filter for `extra_skill_dirs.txt`
  (mutually exclusive)

Names must be unique against any workgroup-defined template under
`{workgroup_dir}/templates/`. Conflicts raise an error at scan time.
