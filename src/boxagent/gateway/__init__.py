"""Gateway package — composes the dataclass core with HTTP/peer/cluster mixins.

``from boxagent.gateway import Gateway`` keeps working; module-level helpers
(``_create_backend``, ``_ensure_git_repo``, ``sync_skills``, etc.) are
re-exported here for tests that import them directly.
"""

from .core import (
    _GatewayCore,
    _create_backend,
    _ensure_git_repo,
    _infer_platform,
    _parse_peer_message,
    _supports_persistent_session,
    sync_skills,
    logger,
)
from .cluster_api import ClusterApiMixin
from .http_api import HttpApiMixin
from .peer import PeerMixin
from .workgroup_api import WorkgroupApiMixin
from boxagent.transports.web.server import WebServerMixin

# Re-exported so tests can ``patch("boxagent.gateway.X")`` to override the
# class used by ``_create_backend`` / ``_start_bot``. Core code looks these
# up via ``boxagent.gateway`` (not its own local imports) for that reason.
from boxagent.agent.claude_process import ClaudeProcess
from boxagent.router import Router
from boxagent.watchdog import Watchdog


class Gateway(
    WebServerMixin,
    HttpApiMixin,
    WorkgroupApiMixin,
    PeerMixin,
    ClusterApiMixin,
    _GatewayCore,
):
    """Top-level Gateway. State + lifecycle live in ``_GatewayCore``;
    request handlers come from the mixins."""
    pass


__all__ = [
    "Gateway",
    "_GatewayCore",
    "_create_backend",
    "_ensure_git_repo",
    "_infer_platform",
    "_parse_peer_message",
    "_supports_persistent_session",
    "sync_skills",
    "logger",
]
