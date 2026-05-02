"""Hub-and-spoke clustering for BoxAgent.

A *host* node accepts WebSocket connections from *satellite* nodes at
``/api/sat/ws``.  Each satellite registers its bots with a ``hello`` frame.
The host then forwards web-UI HTTP requests bound for a remote bot over
the WS to the satellite that owns it, using a generic RPC envelope.

Wire protocol::

  # Sat → Host (immediately after open)
  {"type": "hello", "machine_id": "pc", "token": "...", "bots": [...]}

  # Host → Sat (a request)
  {"type": "rpc", "id": "<uuid>", "method": "GET",
   "path": "/api/history", "query": {"bot": "x", "chat_id": "y"}, "body": null}

  # Sat → Host (non-streaming response)
  {"type": "rpc_resp", "id": "<uuid>", "status": 200, "body": {...}}

  # Sat → Host (streaming response, e.g. SSE)
  {"type": "rpc_stream", "id": "<uuid>", "data": "<sse data line>"}
  ...
  {"type": "rpc_end",    "id": "<uuid>"}

  # Either direction
  {"type": "ping"}  /  {"type": "pong"}
"""

from .registry import RemoteBot, SatelliteRegistry, SatelliteSession
from .sat_client import SatelliteClient
from .tunnel import ClusterTunnel

__all__ = ["RemoteBot", "SatelliteRegistry", "SatelliteSession", "SatelliteClient", "ClusterTunnel"]
