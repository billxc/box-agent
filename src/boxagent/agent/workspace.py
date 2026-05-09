"""Workspace setup helpers — git skeleton + skill symlinks.

Used by both ``AgentManager`` and ``WorkgroupManager`` (to prepare bot /
specialist workspaces). Module-level so both import directly.

Backend-aware (the skill dir is ``.agents/skills/`` for codex-cli vs
``.claude/skills/`` for the rest), so this isn't a generic ``utils``
helper — it knows BoxAgent backend conventions.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_git_repo(workspace: Path) -> bool:
    """Ensure ``workspace`` is a git repo (minimal skeleton).

    Claude Code uses git root to locate ``.claude/skills/``. If the
    workspace lives inside a parent git repo the skills directory won't
    be found. Creating a minimal ``.git`` makes the workspace its own
    git root so skill discovery works correctly.

    Returns *True* if a new ``.git`` was created.
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    git_dir = workspace / ".git"
    if git_dir.exists():
        return False
    git_dir.mkdir(exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "objects").mkdir(exist_ok=True)
    (git_dir / "refs").mkdir(exist_ok=True)
    (git_dir / "refs" / "heads").mkdir(exist_ok=True)
    logger.info("Created minimal .git in %s (Claude Code needs git root to discover skills)", workspace)
    return True


def sync_skills(
    workspace: str,
    extra_skill_dirs: list[str],
    ai_backend: str = "claude-cli",
) -> list[str]:
    """Symlink skill subdirs into the backend-specific skills directory.

    - Claude-style backends (claude-cli, agent-sdk-claude): ``{workspace}/.claude/skills/``
    - Codex CLI backend: ``{workspace}/.agents/skills/``
    """
    skills_root = ".agents" if ai_backend == "codex-cli" else ".claude"
    skills_dir = Path(workspace) / skills_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    for entry in skills_dir.iterdir():
        if entry.is_symlink() and not entry.exists():
            logger.info("Removing broken skill symlink: %s", entry)
            entry.unlink()

    linked = []
    for src_dir in extra_skill_dirs:
        src_path = Path(src_dir).expanduser().resolve()
        if not src_path.is_dir():
            logger.warning("Skill dir not found: %s", src_path)
            continue
        for child in sorted(src_path.iterdir()):
            if not child.is_dir():
                continue
            link = skills_dir / child.name
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                continue  # don't overwrite real dirs
            link.symlink_to(child)
            linked.append(child.name)
    return linked
