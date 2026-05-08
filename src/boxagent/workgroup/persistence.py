"""Workgroup specialist persistence — read/write workgroup_specialists.yaml."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from boxagent.config import SpecialistConfig

logger = logging.getLogger(__name__)


def specialists_file(local_dir: Path) -> Path:
    return local_dir / "workgroup_specialists.yaml"


def load_saved_specialists(
    local_dir: Path, workgroup_name: str,
) -> dict[str, SpecialistConfig]:
    """Load dynamically created specialists from local storage."""
    path = specialists_file(local_dir)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        entries = data.get(workgroup_name, {})
        result = {}
        for specialist_name, specialist_raw in entries.items():
            result[specialist_name] = SpecialistConfig(
                name=specialist_name,
                model=specialist_raw.get("model", ""),
                workspace=specialist_raw.get("workspace", ""),
                ai_backend=specialist_raw.get("ai_backend", ""),
                display_name=specialist_raw.get("display_name", specialist_name),
                extra_skill_dirs=list(specialist_raw.get("extra_skill_dirs", []) or []),
                template=specialist_raw.get("template", "") or "",
            )
        return result
    except Exception as e:
        logger.warning("Failed to load saved specialists: %s", e)
        return {}


def save_specialist(
    local_dir: Path, workgroup_name: str, specialist: SpecialistConfig,
) -> None:
    """Persist a dynamically created specialist to local storage."""
    path = specialists_file(local_dir)
    data: dict = {}
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            pass
    data.setdefault(workgroup_name, {})[specialist.name] = {
        "model": specialist.model,
        "workspace": specialist.workspace,
        "ai_backend": specialist.ai_backend,
        "display_name": specialist.display_name,
        "extra_skill_dirs": list(specialist.extra_skill_dirs),
        "template": specialist.template,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False)


def remove_saved_specialist(
    local_dir: Path, workgroup_name: str, specialist_name: str,
) -> None:
    """Remove a specialist from the saved workgroup_specialists.yaml."""
    path = specialists_file(local_dir)
    if not path.is_file():
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        workgroup_data = data.get(workgroup_name, {})
        if specialist_name in workgroup_data:
            del workgroup_data[specialist_name]
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False)
    except Exception as e:
        logger.warning("Failed to remove saved specialist '%s': %s", specialist_name, e)
