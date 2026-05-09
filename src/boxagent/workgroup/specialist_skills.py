"""Workgroup template-skill symlink helpers.

Extracted from WorkgroupManager so the orchestrator stays focused on
lifecycle.
"""

from __future__ import annotations

import logging
from pathlib import Path

from boxagent.agent.workspace import sync_skills
from boxagent.workgroup.template_loader import TemplateInfo, filter_skill_subdirs

logger = logging.getLogger(__name__)


def apply_template_skills(
    workspace: str,
    template_info: TemplateInfo,
    ai_backend: str,
) -> None:
    """Symlink template-provided skills into specialist workspace.

    Two sources, both routed through ``sync_skills`` (parent-of-skills convention):
      1. template/skills/    — not subject to allow/block filter
      2. template/extra_skill_dirs.txt entries — filtered by allow/block
    """
    # 1. Inline template skills (symlink each subdir directly).
    if template_info.skills_dir and template_info.skills_dir.is_dir():
        sync_skills(workspace, [str(template_info.skills_dir)], ai_backend)
    # 2. External skill dirs with allow/block filter.
    for parent in template_info.extra_skill_dirs:
        selected = filter_skill_subdirs(
            parent, template_info.skill_allows, template_info.skill_blocks
        )
        if not selected:
            continue
        if (
            template_info.skill_allows is None
            and template_info.skill_blocks is None
        ):
            sync_skills(workspace, [str(parent)], ai_backend)
        else:
            symlink_template_skills(workspace, selected, ai_backend)


def symlink_template_skills(
    workspace: str,
    skill_dirs: list[Path],
    ai_backend: str,
) -> None:
    """Symlink individual skill subdirs into the specialist's skills root."""
    skills_root_name = ".agents/skills" if "codex" in ai_backend else ".claude/skills"
    skills_root = Path(workspace) / skills_root_name
    skills_root.mkdir(parents=True, exist_ok=True)
    for src in skill_dirs:
        target = skills_root / src.name
        if target.is_symlink() or target.exists():
            if target.is_symlink():
                try:
                    target.unlink()
                except Exception:
                    pass
            else:
                # Don't overwrite a real directory.
                continue
        try:
            target.symlink_to(src.resolve(), target_is_directory=True)
        except Exception as e:
            logger.warning(
                "Failed to symlink template skill %s → %s: %s", src, target, e
            )
