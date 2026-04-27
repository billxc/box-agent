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
    wg_name: str,
    specialists: list[str],
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

    if specialists:
        specialists_block = "\n".join(f"- `{name}`" for name in specialists)
    else:
        specialists_block = "_No specialists configured yet._"

    worktrees_dir = str(Path(workspace).parent / "worktrees")

    # --- System layer (overwritten every startup) ---

    # .claude/CLAUDE.md
    content = ADMIN_CLAUDE_MD.format(
        wg_name=wg_name,
        specialists_block=specialists_block,
        superboss_ref=SUPERBOSS_REF,
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
    sp_name: str,
    wg_name: str,
) -> list[str]:
    """Seed template files into specialist workspace.

    System-layer files are overwritten to stay current.
    User-layer files are created only if they don't exist.
    Returns list of written file paths (relative to workspace).
    """
    if not workspace:
        return []

    ws = Path(workspace)
    written: list[str] = []

    worktrees_dir = str(ws.parent.parent / "worktrees")

    # --- System layer (overwritten every startup) ---

    # .claude/CLAUDE.md
    content = SPECIALIST_CLAUDE_MD.format(
        sp_name=sp_name,
        wg_name=wg_name,
        supercrew_ref=SUPERCREW_REF,
        worktrees_dir=worktrees_dir,
    )
    if _write_always(ws / ".claude" / "CLAUDE.md", content):
        written.append(".claude/CLAUDE.md")

    # .claude/skills/supercrew/SKILL.md
    skill = SPECIALIST_SKILL_MD.format(supercrew_ref=SUPERCREW_REF)
    if _write_always(ws / ".claude" / "skills" / "supercrew" / "SKILL.md", skill):
        written.append(".claude/skills/supercrew/SKILL.md")

    # .claude/skills/supercrew/references/templates.md
    if _write_always(
        ws / ".claude" / "skills" / "supercrew" / "references" / "templates.md",
        SPECIALIST_TEMPLATES_MD,
    ):
        written.append(".claude/skills/supercrew/references/templates.md")

    if written:
        logger.info("Seeded specialist workspace %s (%s): %s", workspace, sp_name, written)
    return written
