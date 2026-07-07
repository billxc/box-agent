"""迁到 Starlette + Hypercorn 后的 Web UI server 端到端测试。

起真的 ``WebHttpServer.start()``（Hypercorn 在后台 task），用 httpx 打 HTTP/1.1
与 HTTP/2 两条路，验证：
  1. HTTP/2 (h2c，prior-knowledge) 真的能协商上——本次迁移的核心目的。
  2. 路由 / 鉴权 / JSON 响应行为与旧 aiohttp 版对等（localhost 放行、静态 index、
     缺参 400、未授权 401）。

这是唯一一处起真 Hypercorn 的测试；其余 web 测试直接调 handler。
"""
from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from boxagent.transports.web.server import WebHttpServer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_server(tmp_path: Path, port: int) -> WebHttpServer:
    config = SimpleNamespace(
        web_token="", web_trust_header="", web_host="127.0.0.1", web_port=port,
        bots={}, node_id="node-a",
    )
    topology = MagicMock()
    topology.local_machine_id.return_value = "node-a"
    topology.guest_registry = None
    topology.guest_client = None
    topology.local_role.return_value = "host"
    topology.local_bot_descriptors.return_value = []
    cluster_rpc = MagicMock()
    cluster_rpc.dispatch_machine_request = AsyncMock(return_value=None)
    return WebHttpServer(
        config=config,
        local_dir=tmp_path,
        config_dir=tmp_path,
        storage=None,
        web_channels={},
        pools={},
        topology=topology,
        cluster_rpc=cluster_rpc,
        cluster_routes=None,
        message_bus=None,
    )


@pytest.fixture
async def running_server(tmp_path):
    port = _free_port()
    server = _make_server(tmp_path, port)
    await server.start()
    # 等 Hypercorn bind 端口就绪。用 async 客户端探测——同步 httpx 会阻塞事件循环，
    # 反而让同循环上的 Hypercorn serve task 跑不起来（探测永远失败）。
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=0.5) as probe:
        for _ in range(100):
            try:
                await probe.get(base + "/api/version")
                break
            except Exception:
                await asyncio.sleep(0.05)
    yield server, base, port
    await server.stop()


@pytest.mark.asyncio
async def test_http2_negotiated_prior_knowledge(running_server):
    """curl --http2-prior-knowledge 应协商到 HTTP/2（h2c 明文，本次迁移的核心目的）。

    用 curl 而非 httpx：httpx 的 ``http2=True`` 在明文连接上不会发 h2 prior-knowledge
    前导，只在 TLS 上靠 ALPN 升级；curl ``--http2-prior-knowledge`` 直接跑 h2c，正是
    devtunnel 终结 TLS 后对本机 origin 的连法。"""
    _server, base, _port = running_server
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl not available")
    proc = await asyncio.create_subprocess_exec(
        curl, "-s", "-o", "/dev/null", "-w", "%{http_version}",
        "--http2-prior-knowledge", base + "/api/version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    http_version = stdout.decode().strip()
    # curl 报 "2" 表示 HTTP/2 协商成功。
    assert http_version == "2", f"expected HTTP/2, curl reported {http_version!r}"


@pytest.mark.asyncio
async def test_http1_still_works(running_server):
    """HTTP/1.1 客户端仍应正常（h2c 是 upgrade/prior-knowledge，不强制）。"""
    _server, base, _port = running_server
    async with httpx.AsyncClient(http2=False, timeout=5.0) as client:
        response = await client.get(base + "/api/version")
    assert response.status_code == 200
    assert response.http_version == "HTTP/1.1"
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_static_index_served(running_server):
    """``/`` 返回 index.html（static mount 未遮蔽 API 路由）。"""
    _server, base, _port = running_server
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(base + "/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_missing_params_returns_400(running_server):
    """缺 bot/chat_id/machine → 400（鉴权对 localhost 放行后进 handler 校验）。"""
    _server, base, _port = running_server
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(base + "/api/history")
    assert response.status_code == 400
    assert response.json()["ok"] is False


@pytest.mark.asyncio
async def test_non_localhost_without_token_unauthorized(tmp_path):
    """带 token 时，非 localhost 且无 token 的请求返回 401（鉴权行为对等）。

    直接调 handler 验证鉴权分支——用一个 client.host 非 localhost 的假 request。
    """
    server = _make_server(tmp_path, _free_port())
    server.config.web_token = "secret"
    request = MagicMock()
    request.client = SimpleNamespace(host="10.0.0.9")
    request.headers = {}
    request.query_params = {}
    response = await server._handle_web_bots(request)
    assert response.status_code == 401
