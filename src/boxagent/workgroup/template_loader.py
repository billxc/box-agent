"""Specialist template discovery, parsing, and skill filtering.

A template is a directory containing:
    description.md          (required) — single-line description
    CLAUDE.md               (required) — prompt fragment appended to system layer
    skills/                 (optional) — each subdir is a skill, symlinked into specialist
    extra_skill_dirs.txt    (optional) — list of external skill parent dirs
    extra_skill_allows.txt  (optional) — only allow these skill names from extra_skill_dirs
    extra_skill_blocks.txt  (optional) — exclude these skill names from extra_skill_dirs
                                          (mutually exclusive with allows)

Templates live in two locations:
    - builtin: shipped with code (src/boxagent/workgroup/templates/builtin_templates/)
    - workgroup: user-defined ({workgroup_dir}/templates/)

Names must be unique across both locations; conflicts raise ValueError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TemplateInfo:
    name: str
    description: str
    source_dir: Path
    claude_md_path: Path
    skills_dir: Path | None = None
    extra_skill_dirs: list[Path] = field(default_factory=list)
    skill_allows: set[str] | None = None
    skill_blocks: set[str] | None = None

    def read_claude_md(self) -> str:
        return self.claude_md_path.read_text(encoding="utf-8")


def _read_lines(path: Path) -> list[str]:
    """Read a text file, return non-empty non-comment lines (stripped)."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _resolve_skill_dir(raw: str, boxagent_dir: Path) -> Path:
    """Resolve a path from extra_skill_dirs.txt anchored to boxagent_dir."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = boxagent_dir / p
    return p.resolve()


def _parse_template_dir(
    template_dir: Path, boxagent_dir: Path
) -> TemplateInfo:
    """Parse a single template directory into a TemplateInfo. Raises ValueError on errors."""
    name = template_dir.name

    description_path = template_dir / "description.md"
    if not description_path.is_file():
        raise ValueError(
            f"template '{name}' missing required file: description.md"
        )
    claude_md_path = template_dir / "CLAUDE.md"
    if not claude_md_path.is_file():
        raise ValueError(
            f"template '{name}' missing required file: CLAUDE.md"
        )

    description = description_path.read_text(encoding="utf-8").strip()
    # Take first non-empty line as description if multi-line.
    for line in description.splitlines():
        if line.strip():
            description = line.strip()
            break

    skills_dir: Path | None = None
    inline_skills = template_dir / "skills"
    if inline_skills.is_dir():
        skills_dir = inline_skills

    extra_skill_dirs: list[Path] = []
    extra_path_file = template_dir / "extra_skill_dirs.txt"
    if extra_path_file.is_file():
        for raw in _read_lines(extra_path_file):
            resolved = _resolve_skill_dir(raw, boxagent_dir)
            if not resolved.is_dir():
                raise ValueError(
                    f"template '{name}': extra_skill_dirs.txt entry "
                    f"'{raw}' resolves to non-existent dir: {resolved}"
                )
            extra_skill_dirs.append(resolved)

    allows_path = template_dir / "extra_skill_allows.txt"
    blocks_path = template_dir / "extra_skill_blocks.txt"
    if allows_path.is_file() and blocks_path.is_file():
        raise ValueError(
            f"template '{name}': extra_skill_allows.txt and extra_skill_blocks.txt "
            f"are mutually exclusive; only one may be present"
        )

    skill_allows: set[str] | None = None
    skill_blocks: set[str] | None = None
    if allows_path.is_file():
        skill_allows = set(_read_lines(allows_path))
    elif blocks_path.is_file():
        skill_blocks = set(_read_lines(blocks_path))

    return TemplateInfo(
        name=name,
        description=description,
        source_dir=template_dir,
        claude_md_path=claude_md_path,
        skills_dir=skills_dir,
        extra_skill_dirs=extra_skill_dirs,
        skill_allows=skill_allows,
        skill_blocks=skill_blocks,
    )


def _scan_dir(root: Path, boxagent_dir: Path) -> dict[str, TemplateInfo]:
    """Scan a templates root directory; return name→TemplateInfo. Empty if root missing."""
    if not root.is_dir():
        return {}
    out: dict[str, TemplateInfo] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        info = _parse_template_dir(child, boxagent_dir)
        out[info.name] = info
    return out


def discover_templates(
    workgroup_templates_dir: Path,
    builtin_templates_dir: Path,
    boxagent_dir: Path,
) -> dict[str, TemplateInfo]:
    """Return all available templates from builtin + workgroup roots.

    Names must be unique across both roots. Conflict raises ValueError.
    Missing required files inside a template raise ValueError.
    """
    builtin = _scan_dir(builtin_templates_dir, boxagent_dir)
    workgroup = _scan_dir(workgroup_templates_dir, boxagent_dir)

    conflicts = set(builtin) & set(workgroup)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(
            f"template name conflict between builtin and workgroup: {names}"
        )

    merged = dict(builtin)
    merged.update(workgroup)
    return merged


def get_template(
    name: str,
    workgroup_templates_dir: Path,
    builtin_templates_dir: Path,
    boxagent_dir: Path,
) -> TemplateInfo:
    """Look up a single template by name. Raises ValueError if not found."""
    templates = discover_templates(
        workgroup_templates_dir, builtin_templates_dir, boxagent_dir
    )
    if name not in templates:
        available = ", ".join(sorted(templates)) or "(none)"
        raise ValueError(
            f"template '{name}' not found. Available: {available}"
        )
    return templates[name]


def filter_skill_subdirs(
    parent: Path,
    allows: set[str] | None,
    blocks: set[str] | None,
) -> list[Path]:
    """Yield sub-skill directories under `parent`, respecting allow/block lists.

    Skill name = subdir.name (last path segment). Names in allows/blocks that
    don't match any actual subdir are silently ignored.
    """
    if not parent.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        if allows is not None and child.name not in allows:
            continue
        if blocks is not None and child.name in blocks:
            continue
        out.append(child)
    return out
