"""Guard against system-prompt advertising MCP tools that don't actually exist.

The bug this catches: ``router/context.py`` builds the admin's system prompt
and tells the model to "use the send_to_agent MCP tool" — but if the
registered tool name in ``tools/builtin/admin.py`` ever drifts away from
that string, the admin AI sees instructions for a phantom tool. Failure
mode is silent and looks like an MCP wiring bug from outside.
"""

from __future__ import annotations

import re

import boxagent.tools.builtin  # noqa: F401  (side-effect: register tools)
from boxagent.router.context import build_session_context
from boxagent.tools import all_tools


# Tool names mentioned by name in the admin/peer system prompt.  When the
# corresponding context branch fires, the LLM is told these names directly,
# so they MUST exist in the registry under the right group.
_PROMPT_TOOLS = {
    "send_to_agent": "admin",
    "send_to_peer": "peer",
}


def _registered(group: str) -> set[str]:
    return {t.name for t in all_tools() if t.group == group}


def test_prompt_tools_exist_in_registry():
    """Every tool the admin prompt references must be registered."""
    missing = []
    for tool_name, group in _PROMPT_TOOLS.items():
        if tool_name not in _registered(group):
            missing.append(f"{tool_name!r} (expected in group={group!r})")
    assert not missing, (
        "Prompt advertises tools that aren't registered: " + ", ".join(missing)
    )


def test_admin_prompt_only_mentions_real_tools():
    """Scan the rendered admin system prompt for ``XXX MCP tool`` patterns
    and assert each name resolves to a registered tool."""
    from boxagent.agent_env import AgentEnv, ChannelInfo, WorkgroupContext

    env = AgentEnv(
        channel=ChannelInfo(platform="web"),
        bot_name="test-admin",
        workgroup=WorkgroupContext(
            role="admin",
            agents=("dev-1",),
            has_peer_channel=True,
        ),
    )
    prompt = build_session_context(env=env)

    # Match `<tool_name> MCP tool` (e.g. "send_to_agent MCP tool").
    mentioned = set(re.findall(r"\b([a-z_][a-z0-9_]*)\s+MCP\s+tool\b", prompt))
    assert mentioned, "Prompt should mention at least one MCP tool name"

    all_registered = {t.name for t in all_tools()}
    bogus = mentioned - all_registered
    assert not bogus, (
        f"Prompt mentions tool names that aren't in the registry: {bogus}. "
        f"Registered: {sorted(all_registered)}"
    )
