#!/usr/bin/env python3
"""Scan src/boxagent/ for short / abbreviated identifiers (vars, params,
function/class names) and rank them by occurrence count.

Output: docs/naming-audit.md — table of suspect identifiers + file:line of
their definitions, intended as the input to a batch rename.

Heuristics
- "Suspect" = identifier length ≤ 4 OR contains a known-offender token
- Filtered out: well-known short idioms (i, j, k, e, ws, fd, fp, id, ts, …)
  and idiomatic Python/typing names (T, P, R, args, kwargs)
- Module-private dunders/leading underscore are kept (the public name is
  what reviewers care about)

Usage:
    uv run python scripts/naming_audit.py [--write] [--src DIR]

Without --write: prints the audit to stdout. With --write: writes to
docs/naming-audit.md.
"""
from __future__ import annotations

import argparse
import ast
import collections
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Names that are short but conventional and should be ignored.
WHITELIST = {
    # standard idioms
    "i", "j", "k", "n", "m", "x", "y", "z", "_",
    # common python / context vars
    "e", "f", "fp", "fd", "ts", "ms", "ok", "id", "io",
    "ws", "q", "r", "s", "t", "v", "p",
    "args", "kwargs", "self", "cls", "ctx", "env", "msg",
    "log", "url", "uri", "uid", "sid", "pid", "tid", "key",
    "val", "src", "dst", "tag", "now", "out", "ret", "obj",
    "buf", "fmt", "ext", "doc", "row", "col", "len",
    "cwd", "arg", "raw",
    # typing
    "T", "P", "R", "K", "V",
    # http
    "GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD",
    # universal abbreviations
    "proc", "mcp", "rpc", "rec", "app",
}

# Tokens that ALWAYS make a name suspect, even inside a compound (e.g.
# `dt_token` / `sat_client` / `dc_channel`).  Drop these by either spelling
# the full word or replacing with a less ambiguous abbreviation.
KNOWN_OFFENDER_TOKENS = {
    "dt",        # devtunnel
    "dc",        # discord
    "wapp",      # web app
    "tname",     # tunnel name
    "wh",        # webhook
    "mgr",       # manager
    "syn",       # synthesized? unclear
    "ch",        # channel — too easily collides
    "st",        # state? status?
    "cb",        # callback
    "wid", "fid", "cid", "nid", "kid",
}


def _is_offender_token(tok: str) -> bool:
    return tok in KNOWN_OFFENDER_TOKENS


def _is_suspicious_identifier(name: str) -> bool:
    """A name is suspect if EITHER:

    - It is a short single-token name (≤ 3 chars after stripping underscores)
      that isn't whitelisted; OR
    - It contains an offender token (sat, dt, dc, wapp, ...) — flag even if
      the rest of the name is descriptive (e.g. `sat_client` still suspicious).
    """
    n = name.strip("_")
    if not n:
        return False
    if n in WHITELIST:
        return False

    toks = [t for t in n.split("_") if t]
    if any(_is_offender_token(t.lower()) for t in toks):
        return True

    # Single-token, short, not whitelisted.
    if len(toks) == 1 and len(toks[0]) <= 3 and toks[0].lower() not in WHITELIST:
        return True

    return False


@dataclass
class Definition:
    name: str
    file: str
    lineno: int
    kind: str  # class / func / param / attr


@dataclass
class Audit:
    defs: list[Definition] = field(default_factory=list)
    occ_count: collections.Counter = field(default_factory=collections.Counter)

    def add_def(self, d: Definition) -> None:
        self.defs.append(d)

    def add_occurrence(self, name: str) -> None:
        self.occ_count[name] += 1


class _Visitor(ast.NodeVisitor):
    def __init__(self, audit: Audit, file: str):
        self.audit = audit
        self.file = file

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._maybe_def(node.name, node.lineno, "func")
        for arg in (
            node.args.args + node.args.kwonlyargs + node.args.posonlyargs
        ):
            self._maybe_def(arg.arg, arg.lineno, "param")
        if node.args.vararg:
            self._maybe_def(node.args.vararg.arg, node.args.vararg.lineno, "param")
        if node.args.kwarg:
            self._maybe_def(node.args.kwarg.arg, node.args.kwarg.lineno, "param")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # same handling

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            self._maybe_def(arg.arg, node.lineno, "param")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._maybe_def(node.name, node.lineno, "class")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name):
            self._maybe_def(node.target.id, node.lineno, "attr")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for tgt in node.targets:
            self._collect_targets(tgt, node.lineno, "attr")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self._collect_targets(node.target, node.lineno, "loop")
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._collect_targets(item.optional_vars, node.lineno, "with")
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name:
            self._maybe_def(node.name, node.lineno, "except")
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:  # noqa: N802
        self._collect_targets(node.target, getattr(node.target, "lineno", 0), "comp")
        # generic_visit on comprehension is implicit via parent

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        # Count every reference for ranking
        self.audit.add_occurrence(node.id)
        self.generic_visit(node)

    def _collect_targets(self, node: ast.AST, lineno: int, kind: str) -> None:
        """Recurse into Tuple/List/Starred to find all bound Name nodes."""
        if isinstance(node, ast.Name):
            self._maybe_def(node.id, lineno, kind)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self._collect_targets(elt, lineno, kind)
        elif isinstance(node, ast.Starred):
            self._collect_targets(node.value, lineno, kind)

    def _maybe_def(self, name: str, lineno: int, kind: str) -> None:
        if _is_suspicious_identifier(name):
            self.audit.add_def(Definition(name=name, file=self.file, lineno=lineno, kind=kind))


def scan(src_dir: Path) -> Audit:
    audit = Audit()
    for py in sorted(src_dir.rglob("*.py")):
        rel = str(py.relative_to(src_dir.parent.parent))
        # Source FILENAME (without .py) is also an identifier — package
        # consumers import it. Treat the stem like a module name.
        stem = py.stem
        if stem != "__init__" and _is_suspicious_identifier(stem):
            audit.add_def(Definition(name=stem, file=rel, lineno=0, kind="module"))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            print(f"skip {py}: {e}", file=sys.stderr)
            continue
        _Visitor(audit, rel).visit(tree)
    return audit


def render(audit: Audit) -> str:
    # Group by name; sort by (-occurrence_count, name)
    by_name: dict[str, list[Definition]] = collections.defaultdict(list)
    for d in audit.defs:
        by_name[d.name].append(d)

    rows = []
    for name, defs in by_name.items():
        rows.append((name, len(defs), audit.occ_count.get(name, 0), defs))
    # Rank: occurrences desc, then defs desc, then name
    rows.sort(key=lambda r: (-r[2], -r[1], r[0]))

    lines = [
        "# Naming Audit",
        "",
        "Auto-generated by `scripts/naming_audit.py`. Per yait #15 / #16 / #68.",
        "",
        "Suspect identifiers (short or matching known-offender tokens). Each row:",
        "**name** | total references | def sites | first 3 def locations.",
        "",
        "| name | refs | defs | sample locations |",
        "|------|-----:|-----:|------------------|",
    ]
    for name, def_count, occ, defs in rows:
        sample = "; ".join(f"{d.file}:{d.lineno} ({d.kind})" for d in defs[:3])
        lines.append(f"| `{name}` | {occ} | {def_count} | {sample} |")
    lines.append("")
    lines.append(f"Total suspect identifiers: **{len(rows)}**.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true",
                        help="Write to docs/naming-audit.md instead of stdout")
    parser.add_argument("--src", default="src/boxagent",
                        help="Source root to scan (default: src/boxagent)")
    args = parser.parse_args()

    src_dir = Path(args.src).resolve()
    audit = scan(src_dir)
    output = render(audit)

    if args.write:
        out_path = src_dir.parent.parent / "docs" / "naming-audit.md"
        out_path.write_text(output + "\n", encoding="utf-8")
        print(f"wrote {out_path} ({len(audit.defs)} suspect defs)")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
