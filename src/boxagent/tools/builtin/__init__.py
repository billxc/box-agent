"""Built-in BoxAgent tools.

Each module here uses ``@boxagent_tool`` to register a set of tools at
import time. Adapters then enumerate the registry to expose them via the
backend-appropriate transport.
"""

# Importing the modules below has the side effect of populating
# boxagent.tools.registry._TOOLS. Do NOT remove these imports — they're
# the registration trigger.
from boxagent.tools.builtin import telegram_media  # noqa: F401

__all__: list[str] = []
