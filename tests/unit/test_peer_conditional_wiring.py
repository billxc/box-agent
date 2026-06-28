"""方案 A (yait #98 Phase 3b): peer 路由仅在 PeerService 存在时挂载。

没有 workgroups → gateway 不构造 PeerService → ClusterHttpRoutes 只挂
核心的 /api/guest/ws，不挂 /api/peer/*。
"""

from types import SimpleNamespace

from aiohttp import web

from boxagent.cluster.http_routes import ClusterHttpRoutes


def _registered_paths(routes: ClusterHttpRoutes) -> set[str]:
    app = web.Application()
    routes.register(app)
    return {route.resource.canonical for route in app.router.routes()}


def test_guest_ws_always_present_peer_routes_absent_without_peer():
    rpc = SimpleNamespace(handle_guest_ws=lambda request: None)
    routes = ClusterHttpRoutes(peer=None, cluster_rpc=rpc)
    paths = _registered_paths(routes)
    assert "/api/guest/ws" in paths  # core cluster — always mounted
    assert "/api/peer/send" not in paths
    assert "/api/wg/peer/recv" not in paths


def test_peer_routes_present_when_peer_set():
    peer = SimpleNamespace(
        handle_peer_send=lambda request: None,
        handle_wg_peer_recv=lambda request: None,
    )
    rpc = SimpleNamespace(handle_guest_ws=lambda request: None)
    routes = ClusterHttpRoutes(peer=peer, cluster_rpc=rpc)
    paths = _registered_paths(routes)
    assert {"/api/guest/ws", "/api/peer/send", "/api/wg/peer/recv"} <= paths
