"""Hub-and-spoke clustering for BoxAgent.

A *host* node accepts WebSocket connections from *guest* nodes at
``/api/guest/ws`` (legacy alias: ``/api/sat/ws``). Each guest
registers its bots with a ``hello`` frame.
The host then forwards web-UI HTTP requests bound for a remote bot over
the WS to the guest that owns it, using a generic RPC envelope.

Wire protocol::

  # Guest → Host (immediately after open)
  {"type": "hello", "machine_id": "pc", "token": "...", "bots": [...]}

  # Host → Guest (a request)
  {"type": "rpc", "id": "<uuid>", "method": "GET",
   "path": "/api/history", "query": {"bot": "x", "chat_id": "y"}, "body": null}

  # Guest → Host (non-streaming response)
  {"type": "rpc_resp", "id": "<uuid>", "status": 200, "body": {...}}

  # Guest → Host (streaming response, e.g. SSE)
  {"type": "rpc_stream", "id": "<uuid>", "data": "<sse data line>"}
  ...
  {"type": "rpc_end",    "id": "<uuid>"}

  # Either direction
  {"type": "ping"}  /  {"type": "pong"}
"""

from .registry import RemoteBot, GuestRegistry, GuestSession
from .guest_client import GuestClient
from .tunnel import ClusterTunnel

__all__ = ["RemoteBot", "GuestRegistry", "GuestSession", "GuestClient", "ClusterTunnel"]
