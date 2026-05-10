"""Gateway package — local control plane.

Public surface:
- ``Gateway`` — the top-level orchestrator (defined in ``core.py``).
"""

from boxagent.gateway.core import Gateway

__all__ = ["Gateway"]
