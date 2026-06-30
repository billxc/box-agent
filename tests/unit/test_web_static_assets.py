"""Static web asset invariants.

Regression guard for the CSS mojibake bug: the Web UI serves CSS via
aiohttp ``add_static``, which sends ``Content-Type: text/css`` with **no**
``charset``. Without an explicit declaration the browser decodes the
stylesheet with a non-UTF-8 fallback, so CSS ``content`` strings like the
``▸`` list marker (``li::marker``) render as ``â¸``.

Per the CSS spec, ``@charset "UTF-8";`` must be the *first bytes* of the
file (no BOM, no comment, no whitespace before it) to take effect. These
tests lock that in for every CSS file we ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "boxagent" / "transports" / "web" / "static"
CHARSET_PREFIX = b'@charset "UTF-8";'
UTF8_BOM = b"\xef\xbb\xbf"

CSS_FILES = sorted(STATIC_DIR.glob("*.css"))


def test_css_files_exist():
    # If the static dir or CSS files move, the parametrized tests below would
    # silently pass with zero cases — assert we actually found some.
    assert CSS_FILES, f"no CSS files found under {STATIC_DIR}"


@pytest.mark.parametrize("css_path", CSS_FILES, ids=lambda p: p.name)
def test_css_declares_utf8_charset_at_byte_zero(css_path: Path):
    raw = css_path.read_bytes()
    assert not raw.startswith(UTF8_BOM), (
        f"{css_path.name} starts with a UTF-8 BOM; @charset must be the first bytes"
    )
    assert raw.startswith(CHARSET_PREFIX), (
        f"{css_path.name} must begin with {CHARSET_PREFIX!r} (got {raw[:24]!r}). "
        "The @charset rule has to be the very first bytes of the file or the "
        "browser ignores it and may decode the stylesheet as non-UTF-8."
    )
