"""AgentCallback protocol — output interface from any AI backend."""

from typing import Any, Protocol


class AgentCallback(Protocol):
    """Callback interface for agent output events.

    Implemented by channel-specific adapters (e.g., TelegramCallback)
    to route agent output to the user.
    """

    async def on_stream(self, text: str) -> None:
        """Called for each text chunk during streaming."""
        ...

    async def on_tool_call(
        self, name: str, input: dict, result: str
    ) -> None:
        """Called when a tool call is detected.

        Args:
            name: Tool name (e.g., "Bash", "Read")
            input: Parsed tool input dict (accumulated from deltas)
            result: Tool result (empty string during streaming, populated after)
        """
        ...

    async def on_tool_update(
        self,
        tool_call_id: str,
        title: str,
        status: str | None = None,
        input: Any = None,
        output: Any = None,
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
