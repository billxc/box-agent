"""Run the vanilla Web Component tests (node --test) inside the pytest suite.

The Web UI has no build step; components are tested with Node's built-in test
runner + a minimal hand-rolled DOM stub (no jsdom / no npm). This wrapper makes
`uv run pytest` cover the frontend too. Skipped when node isn't installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "src/boxagent/transports/web/static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_component_node_tests():
    test_files = sorted(STATIC_DIR.glob("test/*.test.js"))
    assert test_files, "no frontend *.test.js files found"
    result = subprocess.run(
        ["node", "--test", *[str(p) for p in test_files]],
        cwd=STATIC_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
