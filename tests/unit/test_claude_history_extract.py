"""Tests for ClaudeAgentHistory._extract_records — tool_use / tool_result splitting.

These verify the parsing of one ``SessionMessage`` into ordered display
records (text / tool_call / tool_result / skill_output). Live SDK calls
are tested via the e2e harness; this file pins the conversion logic.
"""

from claude_agent_sdk.types import SessionMessage

from boxagent.history.claude import ClaudeAgentHistory
from boxagent.history.protocol import Message


def _msg(type_: str, content) -> SessionMessage:
    return SessionMessage(
        type=type_,  # type: ignore[arg-type]
        uuid="u-1",
        session_id="s-1",
        message={"role": type_, "content": content},
    )


def _h() -> ClaudeAgentHistory:
    return ClaudeAgentHistory()


def test_text_only_assistant_yields_single_text_record():
    recs = _h()._extract_records(_msg("assistant", [{"type": "text", "text": "hi there"}]))
    assert recs == [Message(role="assistant", text="hi there", ts=0.0)]


def test_string_content_yields_single_record():
    recs = _h()._extract_records(_msg("user", "hello"))
    assert recs == [Message(role="user", text="hello", ts=0.0)]


def test_assistant_text_plus_tool_use_splits_into_two_records():
    recs = _h()._extract_records(_msg("assistant", [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "toolu_x", "name": "Bash",
         "input": {"command": "ls"}},
    ]))
    assert len(recs) == 2
    assert recs[0] == Message(role="assistant", text="Let me check.", ts=0.0)
    assert recs[1].role == "tool_call"
    assert recs[1].tool_id == "toolu_x"
    assert recs[1].name == "Bash"
    assert recs[1].args == {"command": "ls"}


def test_assistant_tool_use_only_no_text_record():
    recs = _h()._extract_records(_msg("assistant", [
        {"type": "tool_use", "id": "toolu_y", "name": "Read",
         "input": {"path": "/x"}},
    ]))
    assert len(recs) == 1
    assert recs[0].role == "tool_call"


def test_user_tool_result_string_content():
    recs = _h()._extract_records(_msg("user", [
        {"type": "tool_result", "tool_use_id": "toolu_x",
         "content": "file.txt\nfile.py", "is_error": False},
    ]))
    assert len(recs) == 1
    assert recs[0].role == "tool_result"
    assert recs[0].tool_id == "toolu_x"
    assert recs[0].ok is True
    assert "file.txt" in recs[0].summary
    assert recs[0].error == ""


def test_tool_result_error_path():
    recs = _h()._extract_records(_msg("user", [
        {"type": "tool_result", "tool_use_id": "toolu_x",
         "content": "command not found", "is_error": True},
    ]))
    assert recs[0].ok is False
    assert recs[0].error == "command not found"
    assert recs[0].summary == ""


def test_tool_result_list_content_joins_text_blocks():
    recs = _h()._extract_records(_msg("user", [
        {"type": "tool_result", "tool_use_id": "tx", "content": [
            {"type": "text", "text": "line1"},
            {"type": "text", "text": "line2"},
        ]},
    ]))
    assert recs[0].summary == "line1\nline2"


def test_non_dict_message_returns_empty():
    msg = SessionMessage(
        type="user",  # type: ignore[arg-type]
        uuid="u-1", session_id="s-1",
        message="not a dict",
    )
    assert _h()._extract_records(msg) == []
