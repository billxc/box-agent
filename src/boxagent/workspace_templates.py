"""Workspace templates for workgroup admin and specialist agents.

Seeds CLAUDE.md, SKILL.md, references/templates.md, and optional HEARTBEAT.md
into freshly created workspaces.  Uses exclusive-create so existing files are
never overwritten.

Templates live as .md files under ``templates/admin/`` and ``templates/specialist/``
(sibling to this module).  They are read at import time and formatted with
:meth:`str.format` when seeding a workspace.

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
# Seed helpers
# ---------------------------------------------------------------------------


def _write_exclusive(path: Path, content: str) -> bool:
    """Write *content* to *path* only if the file doesn't already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(content)
        return True
    except FileExistsError:
        return False


def seed_admin_workspace(
    workspace: str,
    wg_name: str,
    specialists: list[str],
) -> list[str]:
    """Seed template files into admin workspace.

    Never overwrites existing files.  Returns list of created file paths
    (relative to workspace).
    """
    if not workspace:
        return []

    ws = Path(workspace)
    created: list[str] = []

    if specialists:
        specialists_block = "\n".join(f"- `{name}`" for name in specialists)
    else:
        specialists_block = "_No specialists configured yet._"

    # .claude/CLAUDE.md
    content = ADMIN_CLAUDE_MD.format(
        wg_name=wg_name,
        specialists_block=specialists_block,
        superboss_ref=SUPERBOSS_REF,
    )
    if _write_exclusive(ws / ".claude" / "CLAUDE.md", content):
        created.append(".claude/CLAUDE.md")

    # .claude/skills/superboss/SKILL.md
    skill = ADMIN_SKILL_MD.format(superboss_ref=SUPERBOSS_REF)
    if _write_exclusive(ws / ".claude" / "skills" / "superboss" / "SKILL.md", skill):
        created.append(".claude/skills/superboss/SKILL.md")

    # .claude/skills/superboss/references/templates.md
    if _write_exclusive(
        ws / ".claude" / "skills" / "superboss" / "references" / "templates.md",
        ADMIN_TEMPLATES_MD,
    ):
        created.append(".claude/skills/superboss/references/templates.md")

    # HEARTBEAT.md
    if _write_exclusive(ws / "HEARTBEAT.md", HEARTBEAT_MD):
        created.append("HEARTBEAT.md")

    if created:
        logger.info("Seeded admin workspace %s: %s", workspace, created)
    return created


def seed_specialist_workspace(
    workspace: str,
    sp_name: str,
    wg_name: str,
) -> list[str]:
    """Seed template files into specialist workspace.

    Never overwrites existing files.  Returns list of created file paths
    (relative to workspace).
    """
    if not workspace:
        return []

    ws = Path(workspace)
    created: list[str] = []

    # .claude/CLAUDE.md
    content = SPECIALIST_CLAUDE_MD.format(
        sp_name=sp_name,
        wg_name=wg_name,
        supercrew_ref=SUPERCREW_REF,
    )
    if _write_exclusive(ws / ".claude" / "CLAUDE.md", content):
        created.append(".claude/CLAUDE.md")

    # .claude/skills/supercrew/SKILL.md
    skill = SPECIALIST_SKILL_MD.format(supercrew_ref=SUPERCREW_REF)
    if _write_exclusive(ws / ".claude" / "skills" / "supercrew" / "SKILL.md", skill):
        created.append(".claude/skills/supercrew/SKILL.md")

    # .claude/skills/supercrew/references/templates.md
    if _write_exclusive(
        ws / ".claude" / "skills" / "supercrew" / "references" / "templates.md",
        SPECIALIST_TEMPLATES_MD,
    ):
        created.append(".claude/skills/supercrew/references/templates.md")

    if created:
        logger.info("Seeded specialist workspace %s (%s): %s", workspace, sp_name, created)
    return created
