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
