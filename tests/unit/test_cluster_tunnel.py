"""Tests for ClusterTunnel — devtunnel host process supervision (yait #79)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxagent.cluster.tunnel import ClusterTunnel


class _FakeProc:
    """A fake asyncio.subprocess.Process. ``wait()`` blocks until ``die(rc)``
    is called, then returns ``rc``. Test drives lifecycle explicitly."""

    def __init__(self, pid: int = 12345):
        self.pid = pid
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.terminate = MagicMock()
        self.kill = MagicMock()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def die(self, rc: int) -> None:
        self.returncode = rc
        self._exited.set()


class TestRespawn:
    @pytest.mark.asyncio
    async def test_monitor_respawns_when_host_dies(self):
        """Spawn ↦ process dies (rc=1) ↦ monitor task re-spawns a fresh host process."""
        tunnel = ClusterTunnel(name="test-tun", port=9999)
        tunnel._respawn_backoff_seconds = 0.0

        spawned: list[_FakeProc] = []

        async def fake_spawn() -> _FakeProc:
            p = _FakeProc(pid=1000 + len(spawned))
            spawned.append(p)
            return p

        with patch.object(tunnel, "_spawn_host_proc", side_effect=fake_spawn):
            await tunnel._launch_supervised()
            assert len(spawned) == 1

            # Kill the first process — monitor should observe + respawn.
            spawned[0].die(rc=1)
            for _ in range(50):
                if len(spawned) >= 2:
                    break
                await asyncio.sleep(0.01)

        assert len(spawned) == 2, f"expected respawn after death; got {len(spawned)}"
        assert tunnel._host_proc is spawned[1]

        # Cleanup: terminate the alive process + cancel monitor.
        spawned[1].die(rc=0)
        await tunnel.stop()

    @pytest.mark.asyncio
    async def test_stop_does_not_respawn(self):
        """stop() flips the supervisor off so the monitor doesn't respawn
        after intentional shutdown — even though terminate() drives wait()."""
        tunnel = ClusterTunnel(name="test-tun", port=9999)
        tunnel._respawn_backoff_seconds = 0.0

        spawned: list[_FakeProc] = []

        async def fake_spawn() -> _FakeProc:
            p = _FakeProc(pid=2000 + len(spawned))
            spawned.append(p)
            return p

        # Make terminate() actually exit the process so stop()'s wait_for resolves.
        async def real_spawn_and_wire():
            p = await fake_spawn()

            def _terminate():
                p.die(rc=-15)
            p.terminate = MagicMock(side_effect=_terminate)
            return p

        with patch.object(tunnel, "_spawn_host_proc", side_effect=real_spawn_and_wire):
            await tunnel._launch_supervised()
            assert len(spawned) == 1

            await tunnel.stop()
            # Give event loop a tick in case monitor would respawn.
            await asyncio.sleep(0.05)

        assert len(spawned) == 1, "stop() must not trigger respawn"


class TestZombieDetection:
    """devtunnel host child can stay alive while its WS to the devtunnel cloud
    drops (token expiry, network blip, internal error). Supervisor must poll
    `devtunnel show -j` for `hostConnections` and respawn after K zeros — pid
    being alive is not the same as the tunnel being reachable. See yait #96."""

    def _make_spawner(self, spawned: list[_FakeProc], pid_base: int):
        async def fake_spawn() -> _FakeProc:
            process = _FakeProc(pid=pid_base + len(spawned))

            def _terminate():
                process.die(rc=-15)

            process.terminate = MagicMock(side_effect=_terminate)
            spawned.append(process)
            return process

        return fake_spawn

    @pytest.mark.asyncio
    async def test_zero_host_connections_for_K_polls_triggers_respawn(self):
        """K consecutive `hostConnections == 0` readings → terminate + respawn."""
        tunnel = ClusterTunnel(name="test-tun", port=9999)
        tunnel._respawn_backoff_seconds = 0.0
        tunnel._health_check_interval_seconds = 0.01
        tunnel._health_failure_threshold = 2
        tunnel._tunnel_id = "test-tun.asse"

        spawned: list[_FakeProc] = []
        show_payload = {"hostConnections": 0}

        with patch.object(tunnel, "_spawn_host_proc", side_effect=self._make_spawner(spawned, 3000)), \
             patch.object(tunnel, "_show", new=AsyncMock(return_value=show_payload)):
            await tunnel._launch_supervised()
            for _ in range(200):
                if len(spawned) >= 2:
                    break
                await asyncio.sleep(0.01)
            await tunnel.stop()

        assert len(spawned) >= 2, f"expected respawn after K zero polls; got {len(spawned)}"

    @pytest.mark.asyncio
    async def test_healthy_host_never_respawns(self):
        """`hostConnections >= 1` keeps child alive across many poll cycles."""
        tunnel = ClusterTunnel(name="test-tun", port=9999)
        tunnel._respawn_backoff_seconds = 0.0
        tunnel._health_check_interval_seconds = 0.01
        tunnel._health_failure_threshold = 2
        tunnel._tunnel_id = "test-tun.asse"

        spawned: list[_FakeProc] = []
        show_payload = {"hostConnections": 1}

        with patch.object(tunnel, "_spawn_host_proc", side_effect=self._make_spawner(spawned, 4000)), \
             patch.object(tunnel, "_show", new=AsyncMock(return_value=show_payload)):
            await tunnel._launch_supervised()
            await asyncio.sleep(0.15)
            count_after_idle = len(spawned)
            await tunnel.stop()

        assert count_after_idle == 1, f"healthy host must not be respawned; got {count_after_idle}"

    @pytest.mark.asyncio
    async def test_single_zero_poll_does_not_trigger_respawn(self):
        """Threshold K=2 means a single transient 0 reading is tolerated —
        `devtunnel show` itself can blip without the host actually being dead."""
        tunnel = ClusterTunnel(name="test-tun", port=9999)
        tunnel._respawn_backoff_seconds = 0.0
        tunnel._health_check_interval_seconds = 0.01
        tunnel._health_failure_threshold = 2
        tunnel._tunnel_id = "test-tun.asse"

        spawned: list[_FakeProc] = []

        # Alternate 0, 1, 0, 1, ... so threshold is never reached.
        readings = [{"hostConnections": 0}, {"hostConnections": 1}]
        call_idx = {"i": 0}

        async def fake_show(_tid):
            value = readings[call_idx["i"] % len(readings)]
            call_idx["i"] += 1
            return value

        with patch.object(tunnel, "_spawn_host_proc", side_effect=self._make_spawner(spawned, 5000)), \
             patch.object(tunnel, "_show", side_effect=fake_show):
            await tunnel._launch_supervised()
            await asyncio.sleep(0.15)
            count_after_idle = len(spawned)
            await tunnel.stop()

        assert count_after_idle == 1, f"single zero reading must not respawn; got {count_after_idle}"


class TestResolveTunnelId:
    """`_resolve_tunnel_id` uses `devtunnel list -j` (not show) so duplicate
    tunnels in different regions can be detected — show would silently
    return only one and strand guests on the loser. See bug history in
    docstring of tunnel.py."""

    def _list_payload(self, *tunnels: dict | str) -> tuple[int, str, str]:
        normalized = [
            t if isinstance(t, dict) else {"tunnelId": t, "hostConnections": 0}
            for t in tunnels
        ]
        return 0, json.dumps({"tunnels": normalized}), ""

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        tunnel = ClusterTunnel(name="boxagent-cluster")
        with patch.object(tunnel, "_run", new=AsyncMock(return_value=self._list_payload("other.asse", "another.jpe1"))):
            assert await tunnel._resolve_tunnel_id() is None

    @pytest.mark.asyncio
    async def test_single_match_returns_full_id(self):
        tunnel = ClusterTunnel(name="boxagent-cluster")
        with patch.object(
            tunnel,
            "_run",
            new=AsyncMock(return_value=self._list_payload("boxagent-cluster.asse", "other.jpe1")),
        ):
            assert await tunnel._resolve_tunnel_id() == "boxagent-cluster.asse"

    @pytest.mark.asyncio
    async def test_multiple_matches_prefers_active_host(self, caplog):
        """Same bare name across regions ⇒ log warning, prefer the one with a live host."""
        tunnel = ClusterTunnel(name="boxagent-cluster")
        payload = self._list_payload(
            {"tunnelId": "boxagent-cluster.jpe1", "hostConnections": 0},
            {"tunnelId": "boxagent-cluster.asse", "hostConnections": 1},
        )
        with patch.object(tunnel, "_run", new=AsyncMock(return_value=payload)):
            with caplog.at_level("WARNING"):
                chosen = await tunnel._resolve_tunnel_id()
        assert chosen == "boxagent-cluster.asse"
        assert any("Multiple tunnels named" in r.message for r in caplog.records)
