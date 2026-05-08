"""Gateway package — local control plane.

Public surface:
- ``Gateway`` — the top-level orchestrator (defined in ``core.py``).

The ``ClaudeProcess`` / ``Router`` / ``Watchdog`` re-exports below exist
purely as test patch targets: ``agent/manager.py`` looks them up via
``boxagent.gateway`` so tests can ``patch("boxagent.gateway.ClaudeProcess")``
to inject mocks.
"""

from boxagent.gateway.core import Gateway

# Test patch targets — see module docstring.
from boxagent.agent.claude_process import ClaudeProcess
from boxagent.router import Router
from boxagent.watchdog import Watchdog

__all__ = ["Gateway"]
