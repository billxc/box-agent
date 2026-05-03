#!/usr/bin/env python3
"""For a given suspect identifier, dump every definition site with the
surrounding source line so the renamer can verify all uses mean the same
thing (collision check before mechanical rename).

Usage:
    uv run python scripts/naming_ambiguity_check.py <name> [<name> ...]

Reads `docs/naming-audit.md` to find def sites for each name, then prints
3-line excerpts at each location. Prints a single warning line per name
if multiple distinct heuristic 'meanings' are detected.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "docs" / "naming-audit.md"


def parse_audit() -> dict[str, list[tuple[str, int, str]]]:
    """name → [(file, lineno, kind), ...]"""
    if not AUDIT.is_file():
        print("naming-audit.md not found — run naming_audit.py --write first", file=sys.stderr)
        sys.exit(1)
    out: dict[str, list[tuple[str, int, str]]] = {}
    text = AUDIT.read_text(encoding="utf-8")
    # Each row: | `name` | refs | defs | sample locations |
    for m in re.finditer(r"^\| `([^`]+)` \| \d+ \| \d+ \| (.+?) \|$", text, re.MULTILINE):
        name = m.group(1)
        locs = m.group(2)
        sites = []
        for site in re.finditer(r"([\w./-]+\.py):(\d+) \(([^)]+)\)", locs):
            sites.append((site.group(1), int(site.group(2)), site.group(3)))
        out[name] = sites
    return out


def excerpt(file: str, lineno: int) -> str:
    p = REPO / file
    if not p.is_file() or lineno <= 0:
        return f"(no source: {file}:{lineno})"
    lines = p.read_text(encoding="utf-8").splitlines()
    a = max(0, lineno - 2)
    b = min(len(lines), lineno + 1)
    out = []
    for i in range(a, b):
        prefix = ">> " if i + 1 == lineno else "   "
        out.append(f"{prefix}{file}:{i+1}: {lines[i].rstrip()}")
    return "\n".join(out)


def heuristic_meaning(line: str, name: str) -> str:
    """Cheap meaning-bucket from the assignment RHS or annotation."""
    line_l = line.lower()
    # Look for type annotation
    m = re.search(rf"\b{re.escape(name)}\s*:\s*([\w\[\].]+)", line)
    if m:
        return f"type={m.group(1)}"
    # Look for assignment RHS
    m = re.search(rf"\b{re.escape(name)}\s*=\s*(.+?)$", line)
    if m:
        rhs = m.group(1).strip().rstrip(",")
        # truncate
        if len(rhs) > 60:
            rhs = rhs[:60] + "…"
        return f"= {rhs}"
    # Look for "for X in Y" / "as X"
    m = re.search(rf"\bfor\s+{re.escape(name)}\b.*?\sin\s(.+?):", line)
    if m:
        return f"loop in {m.group(1).strip()[:40]}"
    m = re.search(rf"\bas\s+{re.escape(name)}\b", line)
    if m:
        return "as-binding"
    # Function param? — line starts def or has `(...,name`
    if "def " in line and f"{name}" in line:
        return "param"
    return "?"


def check_one(name: str, audit: dict) -> None:
    sites = audit.get(name)
    if not sites:
        print(f"\n## `{name}` — not in audit (or no defs)\n")
        return
    print(f"\n## `{name}` — {len(sites)} def site(s)")
    print()
    meanings: dict[str, list[str]] = defaultdict(list)
    for file, lineno, kind in sites:
        p = REPO / file
        if not p.is_file() or lineno <= 0:
            continue
        line = p.read_text(encoding="utf-8").splitlines()[lineno - 1]
        bucket = heuristic_meaning(line, name)
        meanings[bucket].append(f"{file}:{lineno}")
        print(f"  [{kind:6}] {bucket}")
        print(excerpt(file, lineno))
        print()
    if len(meanings) > 1:
        print(f"  ⚠️  `{name}` has {len(meanings)} distinct heuristic meanings:")
        for k, v in meanings.items():
            print(f"      - {k}  ({len(v)} site(s)) e.g. {v[0]}")
    else:
        print(f"  ✅ all sites share one meaning bucket")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    audit = parse_audit()
    for name in argv:
        check_one(name, audit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
