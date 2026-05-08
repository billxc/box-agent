"""Smoke tests for the boxagent.tools registry."""

import pytest

from boxagent.tools import (
    ToolContext,
    all_tools,
    boxagent_tool,
    env_capabilities,
    tools_for,
)
from boxagent.tools.registry import _reset_for_tests


@pytest.fixture(autouse=True)
def _isolate():
    """Each test starts with an empty registry."""
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestRegistration:
    def test_decorator_registers_with_metadata(self):
        @boxagent_tool(
            name="hello",
            group="base",
            description="Say hello",
            schema={"name": str},
        )
        async def hello(args, ctx):
            return f"hi {args['name']}"

        tools = all_tools()
        assert len(tools) == 1
        t = tools[0]
        assert t.name == "hello"
        assert t.group == "base"
        assert t.description == "Say hello"
        assert t.schema == {"name": str}
        assert t.requires == []
        assert t.handler is hello

    def test_duplicate_name_raises(self):
        @boxagent_tool(name="dup", group="base", description="")
        async def first(args, ctx):
            return ""

        with pytest.raises(ValueError, match="Duplicate"):
            @boxagent_tool(name="dup", group="base", description="")
            async def second(args, ctx):
                return ""

    def test_requires_passes_through(self):
        @boxagent_tool(
            name="t", group="telegram", description="",
            requires=["telegram"],
        )
        async def fn(args, ctx):
            return ""

        assert all_tools()[0].requires == ["telegram"]


class TestFilter:
    def setup_method(self):
        @boxagent_tool(name="b1", group="base", description="")
        async def b1(args, ctx): return ""

        @boxagent_tool(name="t1", group="telegram", description="",
                       requires=["telegram"])
        async def t1(args, ctx): return ""

        @boxagent_tool(name="a1", group="admin", description="",
                       requires=["workgroup_admin"])
        async def a1(args, ctx): return ""

        @boxagent_tool(name="ap", group="admin", description="",
                       requires=["workgroup_admin", "telegram"])
        async def ap(args, ctx): return ""

    def test_filter_by_group(self):
        names = {t.name for t in tools_for(group="telegram")}
        assert names == {"t1"}

    def test_filter_by_env_caps(self):
        names = {t.name for t in tools_for(env_caps={"telegram"})}
        assert names == {"b1", "t1"}

    def test_filter_by_env_caps_admin_alone_excludes_combo(self):
        names = {t.name for t in tools_for(env_caps={"workgroup_admin"})}
        # ap requires telegram + workgroup_admin — should be excluded
        assert names == {"b1", "a1"}

    def test_filter_by_env_caps_combo(self):
        names = {t.name for t in tools_for(env_caps={"workgroup_admin", "telegram"})}
        assert names == {"b1", "t1", "a1", "ap"}

    def test_no_filter_returns_all(self):
        assert len(tools_for()) == 4


class TestEnvCapabilities:
    def test_pulls_caps_from_env_attrs(self):
        from types import SimpleNamespace
        env = SimpleNamespace(
            has_telegram=True,
            is_workgroup_admin=False,
            has_peer_channel=True,
        )
        assert env_capabilities(env) == {"telegram", "peer_channel"}

    def test_none_env_yields_empty(self):
        assert env_capabilities(None) == set()


class TestToolContext:
    def test_required_fields(self):
        ctx = ToolContext(bot_name="b", chat_id="c")
        assert ctx.bot_name == "b"
        assert ctx.chat_id == "c"
        assert ctx.gateway is None  # default
