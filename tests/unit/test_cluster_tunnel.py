"""Tests for ClusterTunnel — devtunnel host process supervision (yait #79)."""

import asyncio
from unittest.mock import MagicMock, patch

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
        """Spawn ↦ proc dies (rc=1) ↦ monitor task re-spawns a fresh host proc."""
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

            # Kill the first proc — monitor should observe + respawn.
            spawned[0].die(rc=1)
            for _ in range(50):
                if len(spawned) >= 2:
                    break
                await asyncio.sleep(0.01)

        assert len(spawned) == 2, f"expected respawn after death; got {len(spawned)}"
        assert tunnel._host_proc is spawned[1]

        # Cleanup: terminate the alive proc + cancel monitor.
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

        # Make terminate() actually exit the proc so stop()'s wait_for resolves.
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
