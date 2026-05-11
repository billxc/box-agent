"""AgentCallback protocol — output interface from any AI backend."""

from typing import Any, Protocol


class AgentCallback(Protocol):
    """Callback interface for agent output events.

    Implemented by channel-specific adapters (e.g., TelegramCallback)
    to route agent output to the user.
    """

    async def on_stream(self, text: str, parent_tool_id: str = "") -> None:
        """Called for each text chunk during streaming.

        Args:
            text: The chunk text.
            parent_tool_id: Non-empty when this chunk belongs to a subagent
                spawned by a Task tool — channels can render it as
                subordinate (e.g. collapsed/grayed) rather than top-level.
        """
        ...

    async def on_tool_call(
        self, name: str, input: dict, result: str, tool_id: str = "",
        parent_tool_id: str = "",
    ) -> None:
        """Called when a tool call is detected.

        Args:
            name: Tool name (e.g., "Bash", "Read")
            input: Parsed tool input dict (accumulated from deltas)
            result: Tool result (empty string during streaming, populated after)
            tool_id: Stable per-call id from the upstream backend (used by
                channels that render call/result as a paired card; empty when
                the backend doesn't expose one).
            parent_tool_id: Non-empty when this call was made inside a
                subagent — channels render it nested under the parent.
        """
        ...

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: Any = None,
        output: Any = None,
        parent_tool_id: str = "",
    ) -> None:
        """Called when a backend exposes richer tool call lifecycle updates."""
        ...

    async def on_error(self, error: str) -> None:
        """Called when an error occurs during agent execution."""
        ...

    async def on_file(self, path: str, caption: str = "") -> None:
        """Called when the agent produces a file. V1: no-op stub."""
        ...

    async def on_image(self, path: str, caption: str = "") -> None:
        """Called when the agent produces an image. V1: no-op stub."""
        ...
