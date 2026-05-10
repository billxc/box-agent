"""Surface extra fields from ``claude_agent_sdk`` internals via ``dowhen``.

The SDK's lite-read parsers and message converters drop several JSONL fields
the Web UI / Bot needs:

* ``_to_session_message`` — drops ``timestamp / cwd / gitBranch``
* ``_parse_session_info_from_lite`` — never looks at the
  ``subtype:"away_summary"`` system records that hold each session's recap

Rather than vendoring the full functions (which would silently bit-rot on
SDK upgrades), we attach ``dowhen`` ``<return>`` callbacks that mutate the
returned object in place. The callback only touches the extra attributes;
all the SDK's own logic still runs verbatim.

Failure modes:
- SDK rename / removal of a target function → import-time AttributeError;
  we log a warning and leave the field empty. ``test_sdk_patch.py`` covers
  this in CI.
- SDK signature change that drops a kwarg name we depend on → dowhen will
  raise when invoking the callback. Tests catch it.
"""

from __future__ import annotations

import json as _json
import logging

logger = logging.getLogger(__name__)

_PATCHED = False


def apply() -> None:
    global _PATCHED
    if _PATCHED:
        return
    try:
        from claude_agent_sdk._internal import sessions as sdk_sessions
    except Exception as e:
        logger.warning("history: SDK monkey patch skipped (import failed): %s", e)
        return
    try:
        from dowhen import do
    except Exception as e:
        logger.warning("history: SDK monkey patch skipped (dowhen import failed): %s", e)
        return

    _PATCHED |= _patch_to_session_message(sdk_sessions, do)
    _PATCHED |= _patch_session_info(sdk_sessions, do)


def _patch_to_session_message(sdk_sessions, do) -> bool:
    target = getattr(sdk_sessions, "_to_session_message", None)
    if target is None:
        logger.warning(
            "history: _to_session_message patch skipped — symbol missing "
            "(SDK API changed?)",
        )
        return False

    def attach_jsonl_fields(_retval, entry):
        if _retval is None:
            return
        try:
            _retval.timestamp = entry.get("timestamp")
            _retval.cwd = entry.get("cwd")
            _retval.git_branch = entry.get("gitBranch")
        except Exception:
            pass

    do(attach_jsonl_fields).when(target, "<return>")
    return True


def _patch_session_info(sdk_sessions, do) -> bool:
    """Surface the latest ``subtype:"away_summary"`` content as
    ``SDKSessionInfo.recap``."""
    target = getattr(sdk_sessions, "_parse_session_info_from_lite", None)
    if target is None:
        logger.warning(
            "history: _parse_session_info_from_lite patch skipped — symbol "
            "missing (SDK API changed?)",
        )
        return False

    def attach_recap(_retval, lite):
        if _retval is None:
            return
        try:
            recap = _extract_recap(lite.tail) or _extract_recap(lite.head)
            _retval.recap = recap
        except Exception:
            _retval.recap = ""

    do(attach_recap).when(target, "<return>")
    return True


def _extract_recap(text: str) -> str:
    """Walk lines bottom-up to find the most recent away_summary content."""
    for line in reversed(text.split("\n")):
        if '"away_summary"' not in line:
            continue
        try:
            record = _json.loads(line)
        except Exception:
            continue
        if record.get("subtype") == "away_summary":
            content = record.get("content")
            if isinstance(content, str) and content:
                return content
    return ""
