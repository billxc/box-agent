"""跨机对不兼容/不可达 peer 快速失败的测试。

病根：混版本集群里，向老/离线/不兼容机器发请求会挂满整个请求超时（~30s）。web UI
每个 bot 会发好几个这种跨机请求，每个挂住一个浏览器 HTTP/1.1 连接槽（~6 个），故即使
后端正常，UI 也冻住。

修法：在 hello/welcome 握手时协商 cluster-bus wire 版本，经 machines_snapshot 传播，
让 `dispatch_machine_request` 对版本不匹配的 peer 快速失败（<1ms 返回 502）——绝不发
注定超时的请求。

这些测试锁定：
  1. 带 `v` 的 hello 把协商版本记到 GuestSession 和 bus link；不带 `v` 的 hello 记为 0。
     welcome 帧带上 host 自己的 machine_id（供 guest 对上 host 链路）。
  2. machines_snapshot 描述符携带每台机器的版本。
  3. dispatch 只对**确知不同版本**（正数且 != 本机）返回 502，且从不调用 `request()`；
     版本 0（未知/没学到）**放行**（0 不等于"不兼容"，宁可走一遭也不误杀）。
  4. "更新后重连"的机器刷新到新版本——不再被当成老机器。
  5. guest 判 host 版本取**活连接握手值**（welcome，重连即刷新），盖过会 stale 的快照——
     这是核心 bug 修复：host 升级重连后，guest 不重启也能立刻认它为新版本。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.web import WSMsgType

from boxagent.cluster.cluster_bus import WIRE_VERSION as CLUSTER_BUS_WIRE_VERSION
from boxagent.cluster.registry import GuestRegistry, GuestSession, RemoteBot
from boxagent.cluster.request_reply import RequestReply
from boxagent.cluster.topology_service import TopologyService


# ── 一个先回放脚本化 hello、然后结束的 WebSocketResponse 桩 ──────────


class _ScriptedServerWS:
    """替身，模拟 `handle_ws` 构造的 host 端 WebSocketResponse。

    回放给定的入站帧（仿佛 guest 发来），记录每个出站 send_json，然后 async 迭代结束，
    使 `handle_ws` 返回。"""

    def __init__(self, inbound_frames: list[dict]) -> None:
        self._inbound = [json.dumps(frame) for frame in inbound_frames]
        self.sent: list[dict] = []
        self.closed = False
        self.close_code = 0

    async def prepare(self, request):
        return None

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for raw in self._inbound:
            yield SimpleNamespace(type=WSMsgType.TEXT, data=raw)

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000, message=b""):
        self.closed = True
        self.close_code = code


async def _drive_hello(registry: GuestRegistry, hello: dict) -> tuple[_ScriptedServerWS, GuestSession]:
    """对单个脚本化 hello 跑 handle_ws，返回 (ws, session)。

    handle_ws 会在其脚本流触发的（立即）断连时弹出 session，故我们通过
    on_guest_attached 捕获活的 session——它在 hello 时触发、协商版本已设好——而不是
    事后从 registry.sessions 读回。"""
    captured: dict[str, GuestSession] = {}
    prior_hook = registry.on_guest_attached

    def _capture(machine_id, session):
        captured["session"] = session
        if prior_hook is not None:
            prior_hook(machine_id, session)

    registry.on_guest_attached = _capture
    server_ws = _ScriptedServerWS([hello])
    try:
        with patch("boxagent.cluster.registry.web.WebSocketResponse", return_value=server_ws):
            await registry.handle_ws(request=SimpleNamespace())
    finally:
        registry.on_guest_attached = prior_hook
    return server_ws, captured["session"]


class _RecordingBus:
    """ClusterBus 替身。`links` 是活的 link 映射（detach 会移除一项）；
    `attached_version` 记住每个 link_key 最后一次 attach 的版本，让断言能挺过
    handle_ws 在脚本流结束时做的 detach。"""

    def __init__(self) -> None:
        self.links: dict[str, int] = {}
        self.attached_version: dict[str, int] = {}

    def attach_link(self, link_key, send_frame, *, version=CLUSTER_BUS_WIRE_VERSION):
        self.links[link_key] = version
        self.attached_version[link_key] = version

    def detach_link(self, link_key):
        self.links.pop(link_key, None)


# ── 1. hello 版本协商 ──────────────────────────────────────────────


class TestHelloVersion:
    async def test_hello_with_version_records_it(self):
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus, local_machine_id="host-machine")
        ws, session = await _drive_hello(registry, {
            "type": "hello", "v": CLUSTER_BUS_WIRE_VERSION,
            "machine_id": "devbox", "bots": [],
        })
        # welcome 携带我们的版本 + host 自己的 machine_id（供 guest 对上 host 链路）
        welcome = next(frame for frame in ws.sent if frame.get("type") == "welcome")
        assert welcome["v"] == CLUSTER_BUS_WIRE_VERSION
        assert welcome["machine_id"] == "host-machine"
        # session + bus link 记下协商版本
        assert session.version == CLUSTER_BUS_WIRE_VERSION
        assert bus.attached_version["devbox"] == CLUSTER_BUS_WIRE_VERSION

    async def test_hello_without_version_records_zero(self):
        # 老 peer 的 hello 不带 `v` → 记为 0（不兼容），故对它的请求快速失败而非挂住。
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus)
        _ws, session = await _drive_hello(registry, {
            "type": "hello", "machine_id": "oldbox", "bots": [],
        })
        assert session.version == 0
        assert bus.attached_version["oldbox"] == 0


# ── 2. snapshot 携带版本 ───────────────────────────────────────────────


class TestSnapshotVersion:
    def _topology(self, sessions: dict[str, int]) -> TopologyService:
        config = SimpleNamespace(
            machine_id="host-machine", node_id="", cluster_tunnel=True,
            my_host_index=0, host_priority=["host-machine"],
        )
        registry = GuestRegistry()
        for machine_id, version in sessions.items():
            registry.sessions[machine_id] = GuestSession(
                machine_id=machine_id, ws=None, bots=[RemoteBot(name="b")], version=version,
            )
        host_election = SimpleNamespace(registry=registry, client=None, state="host")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)
        return topology

    def test_collect_machines_stamps_version(self):
        topology = self._topology({"devbox": CLUSTER_BUS_WIRE_VERSION, "oldbox": 0})
        machines = {m["machine_id"]: m for m in topology.collect_machines()}
        # host 把自己盖成当前版本
        assert machines["host-machine"]["version"] == CLUSTER_BUS_WIRE_VERSION
        assert machines["host-machine"]["self"] is True
        # guest 携带其协商版本
        assert machines["devbox"]["version"] == CLUSTER_BUS_WIRE_VERSION
        assert machines["oldbox"]["version"] == 0

    def test_version_for_reads_guest_session(self):
        topology = self._topology({"devbox": CLUSTER_BUS_WIRE_VERSION, "oldbox": 0})
        assert topology.version_for("host-machine") == CLUSTER_BUS_WIRE_VERSION  # 自己
        assert topology.version_for("devbox") == CLUSTER_BUS_WIRE_VERSION
        assert topology.version_for("oldbox") == 0
        assert topology.version_for("ghost") == 0  # 未知机器

    def test_version_for_reads_guest_client_cache(self):
        # guest 从 host 推来的 snapshot 缓存读取 peer 版本。
        config = SimpleNamespace(machine_id="guest-machine", node_id="", cluster_tunnel=True)
        client = SimpleNamespace(remote_machines=[
            {"machine_id": "host-machine", "version": CLUSTER_BUS_WIRE_VERSION},
            {"machine_id": "oldbox", "version": 0},
            {"machine_id": "noversion"},  # 老 host 来的 snapshot → 当作 0
        ])
        host_election = SimpleNamespace(registry=None, client=client, state="guest")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)
        assert topology.version_for("host-machine") == CLUSTER_BUS_WIRE_VERSION
        assert topology.version_for("oldbox") == 0
        assert topology.version_for("noversion") == 0


# ── 3. dispatch 对不兼容 peer 快速失败 ─────────────────────────────────


class _CountingRequestReply(RequestReply):
    """一旦 `request()` 被调用就大声失败的 RequestReply——证明版本预检在任何注定
    发送前就短路了。"""

    def __init__(self, *, topology):
        # 最小 init：我们只跑 dispatch_machine_request，它需要 topology + 一个 bus
        # 供基类的 subscribe 调用。
        bus = SimpleNamespace(
            subscribe=lambda *a, **k: SimpleNamespace(close=lambda: None),
            send=lambda **k: "mid",
        )
        super().__init__(bus=bus, topology=topology, local_web_port=0)
        self.request_calls = 0

    async def request(self, *args, **kwargs):  # type: ignore[override]
        self.request_calls += 1
        raise AssertionError("request() should not be called for an incompatible peer")


def _topology_with_versions(local: str, versions: dict[str, int]):
    return SimpleNamespace(
        local_machine_id=lambda: local,
        guest_registry=None,
        version_for=lambda machine: versions.get(machine, 0),
    )


class TestDispatchFastFail:
    async def test_known_mismatch_returns_502_without_request(self):
        # 确知版本不同（正数且 != 本机）→ 快速失败，绝不发那个注定的请求。
        topology = _topology_with_versions("mbp", {"oldbox": 2})
        rr = _CountingRequestReply(topology=topology)
        request = SimpleNamespace(query={})
        response = await rr.dispatch_machine_request("oldbox", "GET", "/api/history", request)
        assert response is not None
        assert response.status == 502
        assert rr.request_calls == 0  # 从未发出那个注定的请求

    async def test_unknown_version_fails_open(self):
        # 版本 0（未知/没学到/旧构建没报版本但可能同协议）→ 放行，让请求正常走，
        # 而不是误杀（0 不等于"不兼容"）。
        topology = _topology_with_versions("mbp", {"unknownbox": 0})
        bus = SimpleNamespace(
            subscribe=lambda *a, **k: SimpleNamespace(close=lambda: None),
            send=lambda **k: "mid",
        )
        rr = RequestReply(bus=bus, topology=topology, local_web_port=0)
        called = {}

        async def fake_request(machine, method, path, *, query=None, body=None):
            called["machine"] = machine
            return {"status": 200, "body": {"ok": True}}

        rr.request = fake_request  # type: ignore[assignment]
        request = SimpleNamespace(query={})
        response = await rr.dispatch_machine_request("unknownbox", "GET", "/api/history", request)
        assert called["machine"] == "unknownbox"  # 放行了，请求发出去了
        assert response.status == 200

    async def test_local_target_returns_none(self):
        topology = _topology_with_versions("mbp", {})
        rr = _CountingRequestReply(topology=topology)
        request = SimpleNamespace(query={})
        # 本机 → None，让调用方本地处理；不查 version_for
        assert await rr.dispatch_machine_request("mbp", "GET", "/x", request) is None

    async def test_compatible_peer_proceeds_to_request(self):
        # 同版本 peer 不能被快速失败——dispatch 会调用 request()。
        topology = _topology_with_versions("mbp", {"devbox": CLUSTER_BUS_WIRE_VERSION})
        bus = SimpleNamespace(
            subscribe=lambda *a, **k: SimpleNamespace(close=lambda: None),
            send=lambda **k: "mid",
        )
        rr = RequestReply(bus=bus, topology=topology, local_web_port=0)
        called = {}

        async def fake_request(machine, method, path, *, query=None, body=None):
            called["machine"] = machine
            return {"status": 200, "body": {"ok": True}}

        rr.request = fake_request  # type: ignore[assignment]
        request = SimpleNamespace(query={})
        response = await rr.dispatch_machine_request("devbox", "GET", "/api/history", request)
        assert called["machine"] == "devbox"
        assert response.status == 200


# ── 4. 更新并重连的机器刷新到新版本 ───────────────────────────────


class TestReconnectRefreshesVersion:
    async def test_reconnect_upgrades_version(self):
        # 机器先作为老 peer 连入（无 `v`，记为 0），然后更新并以新版本重连——topology
        # 必须报告新版本，使它不再被快速失败。
        bus = _RecordingBus()
        registry = GuestRegistry(cluster_bus=bus)

        config = SimpleNamespace(
            machine_id="host-machine", node_id="", cluster_tunnel=True,
            my_host_index=0, host_priority=["host-machine"],
        )
        host_election = SimpleNamespace(registry=registry, client=None, state="host")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)

        # 第一次连接：老代码，无版本。handle_ws 在脚本断连时弹出 session，故重新
        # 坐回捕获的 session 来模拟仍打开的 link，再查 topology。
        _ws, old_session = await _drive_hello(registry, {
            "type": "hello", "machine_id": "devbox", "bots": [],
        })
        assert old_session.version == 0
        registry.sessions["devbox"] = old_session
        assert topology.version_for("devbox") == 0  # 不兼容 → 会被快速失败

        # devbox 更新并以当前版本重连。
        _ws2, new_session = await _drive_hello(registry, {
            "type": "hello", "v": CLUSTER_BUS_WIRE_VERSION,
            "machine_id": "devbox", "bots": [],
        })
        assert new_session.version == CLUSTER_BUS_WIRE_VERSION
        registry.sessions["devbox"] = new_session
        assert topology.version_for("devbox") == CLUSTER_BUS_WIRE_VERSION  # 已刷新
        assert bus.attached_version["devbox"] == CLUSTER_BUS_WIRE_VERSION


# ── 5. guest 看 host 用活连接握手值，盖过会 stale 的快照 ────────────────


class TestGuestSeesHostVersionLive:
    def _guest_topology(self, *, host_machine_id, host_version, remote_machines):
        config = SimpleNamespace(machine_id="guest-machine", node_id="", cluster_tunnel=True)
        client = SimpleNamespace(
            host_machine_id=host_machine_id,
            host_version=host_version,
            remote_machines=remote_machines,
        )
        host_election = SimpleNamespace(registry=None, client=client, state="guest")
        topology = TopologyService(config=config, web_channels={})
        topology.set_host_election(host_election)
        return topology

    def test_host_version_from_live_handshake_beats_stale_snapshot(self):
        # 这是核心 bug 修复：host 升级重连后 welcome 报 v3，但快照缓存可能还 stale 在
        # 0。version_for(host) 必须取活的 host_version（3），而非 stale 快照（0）。
        topology = self._guest_topology(
            host_machine_id="devbox-xl",
            host_version=CLUSTER_BUS_WIRE_VERSION,           # 活值：welcome 报的
            remote_machines=[{"machine_id": "devbox-xl", "version": 0}],  # stale 快照
        )
        assert topology.version_for("devbox-xl") == CLUSTER_BUS_WIRE_VERSION

    def test_other_guest_still_read_from_snapshot(self):
        # 非 host 的机器（没直连）仍从快照读——那是唯一来源。
        topology = self._guest_topology(
            host_machine_id="devbox-xl",
            host_version=CLUSTER_BUS_WIRE_VERSION,
            remote_machines=[
                {"machine_id": "devbox-xl", "version": CLUSTER_BUS_WIRE_VERSION},
                {"machine_id": "macmini", "version": 0},
            ],
        )
        assert topology.version_for("macmini") == 0

    def test_disconnected_host_version_is_zero(self):
        # 断连后 host_version 清 0、host_machine_id 清空 → version_for 落回快照/0。
        topology = self._guest_topology(
            host_machine_id="", host_version=0, remote_machines=[],
        )
        assert topology.version_for("devbox-xl") == 0


class TestGuestClientWelcome:
    async def test_welcome_sets_live_host_version_and_machine_id(self):
        # guest 收到 welcome 时把 host 的 version + machine_id 存成活值。
        from boxagent.cluster.guest_client import GuestClient
        bus = _RecordingBus()
        client = GuestClient(
            host_url="", host_token="", machine_id="mbp", local_web_port=0, cluster_bus=bus,
        )
        ws = _ScriptedServerWS([{
            "type": "welcome", "v": CLUSTER_BUS_WIRE_VERSION, "machine_id": "devbox-xl",
        }])
        await client._serve(ws)
        assert client.host_version == CLUSTER_BUS_WIRE_VERSION
        assert client.host_machine_id == "devbox-xl"
        assert bus.attached_version["host"] == CLUSTER_BUS_WIRE_VERSION
