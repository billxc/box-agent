"""Tests for boxagent.workgroup.template_loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from boxagent.workgroup.template_loader import (
    discover_templates,
    filter_skill_subdirs,
    get_template,
)


def _make_template(
    root: Path,
    name: str,
    *,
    description: str = "test desc",
    claude_md: str = "# template prompt",
    skills: list[str] | None = None,
    extra_skill_dirs: list[str] | None = None,
    allows: list[str] | None = None,
    blocks: list[str] | None = None,
) -> Path:
    tdir = root / name
    tdir.mkdir(parents=True)
    (tdir / "description.md").write_text(description)
    (tdir / "CLAUDE.md").write_text(claude_md)
    if skills:
        sd = tdir / "skills"
        sd.mkdir()
        for s in skills:
            (sd / s).mkdir()
    if extra_skill_dirs is not None:
        (tdir / "extra_skill_dirs.txt").write_text("\n".join(extra_skill_dirs))
    if allows is not None:
        (tdir / "extra_skill_allows.txt").write_text("\n".join(allows))
    if blocks is not None:
        (tdir / "extra_skill_blocks.txt").write_text("\n".join(blocks))
    return tdir


def test_discover_empty_dirs(tmp_path: Path) -> None:
    out = discover_templates(tmp_path / "workgroup", tmp_path / "builtin", tmp_path)
    assert out == {}


def test_discover_basic(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "planner", description="task decomp")
    out = discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)
    assert set(out) == {"planner"}
    assert out["planner"].description == "task decomp"
    assert out["planner"].skills_dir is None
    assert out["planner"].extra_skill_dirs == []


def test_discover_missing_description_raises(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    bad = workgroup_dir / "broken"
    bad.mkdir()
    (bad / "CLAUDE.md").write_text("x")
    with pytest.raises(ValueError, match="description.md"):
        discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)


def test_discover_missing_claude_md_raises(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    bad = workgroup_dir / "broken"
    bad.mkdir()
    (bad / "description.md").write_text("x")
    with pytest.raises(ValueError, match="CLAUDE.md"):
        discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)


def test_discover_name_conflict_raises(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    builtin_dir = tmp_path / "builtin"
    workgroup_dir.mkdir()
    builtin_dir.mkdir()
    _make_template(workgroup_dir, "planner")
    _make_template(builtin_dir, "planner")
    with pytest.raises(ValueError, match="conflict"):
        discover_templates(workgroup_dir, builtin_dir, tmp_path)


def test_allows_and_blocks_mutex_raises(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "planner", allows=["a"], blocks=["b"])
    with pytest.raises(ValueError, match="mutually exclusive"):
        discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)


def test_extra_skill_dirs_resolution(tmp_path: Path) -> None:
    # boxagent_dir = tmp_path; relative path under it.
    shared = tmp_path / "shared-skills" / "owasp"
    shared.mkdir(parents=True)
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "auditor", extra_skill_dirs=["shared-skills/owasp"])
    out = discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)
    assert out["auditor"].extra_skill_dirs == [shared.resolve()]


def test_extra_skill_dirs_missing_raises(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "auditor", extra_skill_dirs=["does-not-exist"])
    with pytest.raises(ValueError, match="non-existent"):
        discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)


def test_extra_skill_dirs_ignores_blank_and_comments(tmp_path: Path) -> None:
    shared = tmp_path / "shared" / "x"
    shared.mkdir(parents=True)
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(
        workgroup_dir, "t",
        extra_skill_dirs=["", "  ", "# a comment", "shared/x"],
    )
    out = discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)
    assert out["t"].extra_skill_dirs == [shared.resolve()]


def test_get_template_not_found(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "planner")
    with pytest.raises(ValueError, match="not found"):
        get_template("nope", workgroup_dir, tmp_path / "builtin", tmp_path)


def test_filter_skill_subdirs(tmp_path: Path) -> None:
    parent = tmp_path / "p"
    for n in ["a", "b", "c"]:
        (parent / n).mkdir(parents=True)
    # No filter: all
    assert {p.name for p in filter_skill_subdirs(parent, None, None)} == {"a", "b", "c"}
    # allowlist
    assert {p.name for p in filter_skill_subdirs(parent, {"a", "c"}, None)} == {"a", "c"}
    # blocklist
    assert {p.name for p in filter_skill_subdirs(parent, None, {"b"})} == {"a", "c"}
    # Unknown names in allow/block silently ignored
    assert {p.name for p in filter_skill_subdirs(parent, {"a", "zzz"}, None)} == {"a"}


def test_description_takes_first_nonblank_line(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "p", description="\n\nFirst real line\nignored\n")
    out = discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)
    assert out["p"].description == "First real line"


def test_inline_skills_dir_picked_up(tmp_path: Path) -> None:
    workgroup_dir = tmp_path / "workgroup"
    workgroup_dir.mkdir()
    _make_template(workgroup_dir, "p", skills=["one", "two"])
    out = discover_templates(workgroup_dir, tmp_path / "builtin", tmp_path)
    assert out["p"].skills_dir == workgroup_dir / "p" / "skills"
