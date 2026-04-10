"""Version info for BoxAgent.

Reads git commit hash from:
1. direct_url.json (pip/uv git installs bake this automatically)
2. git rev-parse (development installs from git checkout)
"""

import json
import subprocess
from pathlib import Path

__version__ = "0.1.0"


def _git_commit() -> str:
    """Get the git commit hash from install metadata or git."""
    # 1. Try direct_url.json (always present for git-based pip/uv installs)
    try:
        dist_info = Path(__file__).parent.parent / f"boxagent-{__version__}.dist-info" / "direct_url.json"
        if dist_info.is_file():
            data = json.loads(dist_info.read_text())
            commit = data.get("vcs_info", {}).get("commit_id", "")
            if commit:
                return commit[:7]
    except Exception:
        pass

    # 2. Try git (development checkout)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return "unknown"


def version_string() -> str:
    """Return a human-readable version string."""
    return f"boxagent {__version__} (commit: {_git_commit()})"
