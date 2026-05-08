"""Gateway package — composes the dataclass core with HTTP/peer/cluster mixins.

``from boxagent.gateway import Gateway`` is the only public symbol.
"""

from .core import _GatewayCore
from boxagent.agent import (
    BotsMixin,
    _supports_persistent_session,
    sync_skills,
)
from boxagent.cluster.rpc import ClusterRpcMixin
from .http_api import HttpApiMixin
from boxagent.cluster.peer import PeerMixin
from boxagent.cluster.routes import ClusterRoutesMixin
from boxagent.cluster.topology import TopologyMixin
from boxagent.workgroup.routes import WorkgroupApiMixin
from boxagent.transports.web.server import WebServerMixin

# Re-exported so tests can ``patch("boxagent.gateway.X")`` to override the
# class used by ``_create_backend`` / ``_start_bot``. Code in
# ``agent/manager.py`` looks these up via ``boxagent.gateway`` (not its own
# local imports) for that reason.
from boxagent.agent.claude_process import ClaudeProcess
from boxagent.router import Router
from boxagent.watchdog import Watchdog


class Gateway(
    WebServerMixin,
    HttpApiMixin,
    WorkgroupApiMixin,
    PeerMixin,
    ClusterRoutesMixin,
    ClusterRpcMixin,
    TopologyMixin,
    BotsMixin,
    _GatewayCore,
):
    """Top-level Gateway. State + lifecycle live in ``_GatewayCore``;
    request handlers come from the mixins."""
    pass


__all__ = [
    "Gateway",
    "_supports_persistent_session",
    "sync_skills",
]
