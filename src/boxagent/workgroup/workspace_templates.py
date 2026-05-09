"""Workspace templates for workgroup admin and specialist agents.

Two-layer prompt system:
- **System layer** (``.claude/CLAUDE.md``, ``SKILL.md``, ``templates.md``)
  — overwritten on every gateway startup to stay current with code changes.
- **User layer** (``HEARTBEAT.md``, ``BOXAGENT.md``)
  — created once via exclusive-create, never overwritten by the system.
  Users can freely edit these files.

Templates live as .md files under ``templates/admin/`` and ``templates/specialist/``
(sibling to this module).

Adapted from GhostComplex/skills (superboss + supercrew).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill references
# ---------------------------------------------------------------------------

SUPERBOSS_REF = "https://github.com/GhostComplex/skills/blob/main/superboss/SKILL.md"
SUPERCREW_REF = "https://github.com/GhostComplex/skills/blob/main/supercrew/SKILL.md"

# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load(relpath: str) -> str:
    """Read a template file relative to the templates/ directory."""
    return (_TEMPLATES_DIR / relpath).read_text(encoding="utf-8")


# Admin templates
ADMIN_CLAUDE_MD = _load("admin/CLAUDE.md")
ADMIN_SKILL_MD = _load("admin/SKILL.md")
ADMIN_TEMPLATES_MD = _load("admin/templates.md")
HEARTBEAT_MD = _load("admin/HEARTBEAT.md")

# Specialist templates
SPECIALIST_CLAUDE_MD = _load("specialist/CLAUDE.md")
SPECIALIST_SKILL_MD = _load("specialist/SKILL.md")
SPECIALIST_TEMPLATES_MD = _load("specialist/templates.md")

# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _write_always(path: Path, content: str) -> bool:
    """Write *content* to *path*, overwriting if it exists.

    Used for system-layer files that should stay current.
    Returns True if the content changed (or file was created).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def _write_exclusive(path: Path, content: str) -> bool:
    """Write *content* to *path* only if the file doesn't already exist.

    Used for user-layer files that should never be overwritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(content)
        return True
    except FileExistsError:
        return False


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


def seed_admin_workspace(
    workspace: str,
    workgroup_name: str,
) -> list[str]:
    """Seed template files into admin workspace.

    System-layer files are overwritten to stay current.
    User-layer files are created only if they don't exist.
    Returns list of written file paths (relative to workspace).
    """
    if not workspace:
        return []

    ws = Path(workspace)
    written: list[str] = []

    worktrees_dir = str(Path(workspace).parent / "worktrees")

    # --- System layer (overwritten every startup) ---

    # .claude/CLAUDE.md
    content = ADMIN_CLAUDE_MD.format(
        workgroup_name=workgroup_name,
        worktrees_dir=worktrees_dir,
    )
    if _write_always(ws / ".claude" / "CLAUDE.md", content):
        written.append(".claude/CLAUDE.md")

    # .claude/skills/superboss/SKILL.md
    skill = ADMIN_SKILL_MD.format(superboss_ref=SUPERBOSS_REF)
    if _write_always(ws / ".claude" / "skills" / "superboss" / "SKILL.md", skill):
        written.append(".claude/skills/superboss/SKILL.md")

    # .claude/skills/superboss/references/templates.md
    if _write_always(
        ws / ".claude" / "skills" / "superboss" / "references" / "templates.md",
        ADMIN_TEMPLATES_MD,
    ):
        written.append(".claude/skills/superboss/references/templates.md")

    # --- User layer (never overwritten) ---

    # HEARTBEAT.md
    if _write_exclusive(ws / "HEARTBEAT.md", HEARTBEAT_MD):
        written.append("HEARTBEAT.md")

    if written:
        logger.info("Seeded admin workspace %s: %s", workspace, written)
    return written


def seed_specialist_workspace(
    workspace: str,
    specialist_name: str,
    workgroup_name: str,
    template_claude_md_text: str | None = None,
) -> list[str]:
    """Seed template files into specialist workspace.

    System-layer files are overwritten to stay current.
    User-layer files are created only if they don't exist.
    If `template_claude_md_text` is provided, it is appended to the system
    CLAUDE.md as the template layer. Callers normally pass the result of
    `read_template_snapshot(workspace)` so the template snapshot survives
    restarts even if the original template source is later modified.
    Returns list of written file paths (relative to workspace).
    """
    if not workspace:
        return []

    ws = Path(workspace)
    written: list[str] = []

    workgroup_dir = str(ws.parent.parent)
    worktrees_dir = str(ws.parent.parent / "worktrees")

    # --- System layer (overwritten every startup) ---

    # .claude/CLAUDE.md
    content = SPECIALIST_CLAUDE_MD.format(
        specialist_name=specialist_name,
        workgroup_name=workgroup_name,
        supercrew_ref=SUPERCREW_REF,
        worktrees_dir=worktrees_dir,
        workgroup_dir=workgroup_dir,
    )
    if template_claude_md_text:
        content = content.rstrip() + "\n\n" + template_claude_md_text.lstrip()
    if _write_always(ws / ".claude" / "CLAUDE.md", content):
        written.append(".claude/CLAUDE.md")

    # .claude/skills/supercrew/SKILL.md
    skill = SPECIALIST_SKILL_MD.format(supercrew_ref=SUPERCREW_REF, workgroup_name=workgroup_name)
    if _write_always(ws / ".claude" / "skills" / "supercrew" / "SKILL.md", skill):
        written.append(".claude/skills/supercrew/SKILL.md")

    # .claude/skills/supercrew/references/templates.md
    if _write_always(
        ws / ".claude" / "skills" / "supercrew" / "references" / "templates.md",
        SPECIALIST_TEMPLATES_MD,
    ):
        written.append(".claude/skills/supercrew/references/templates.md")

    if written:
        logger.info("Seeded specialist workspace %s (%s): %s", workspace, specialist_name, written)
    return written


# --- Template snapshot ---
# A specialist's CLAUDE.md template layer is captured at create time and stored
# inside the workspace, so subsequent edits to the template source do not
# silently affect already-running specialists. The snapshot lives outside
# .claude/ to avoid being picked up by Claude Code as a CLAUDE.md fragment.

_TEMPLATE_SNAPSHOT_REL = ".boxagent-meta/template-snapshot.md"


def template_snapshot_path(workspace: str | Path) -> Path:
    return Path(workspace) / _TEMPLATE_SNAPSHOT_REL


def write_template_snapshot(workspace: str | Path, claude_md_text: str) -> None:
    path = template_snapshot_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(claude_md_text, encoding="utf-8")


def read_template_snapshot(workspace: str | Path) -> str | None:
    path = template_snapshot_path(workspace)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
