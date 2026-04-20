"""Shared test helpers for unit tests."""

from unittest.mock import AsyncMock


class FakeStdin:
    """Mock stdin that records written data."""

    def __init__(self):
        self.written = b""
        self._closed = False

    def write(self, data: bytes):
        self.written += data

    def close(self):
        self._closed = True


class FakeProcess:
    """Mock asyncio.subprocess.Process with configurable stdout."""

    def __init__(self, stdout_data: bytes, returncode: int = 0):
        self._stdout_data = stdout_data
        self.returncode = None
        self._final_returncode = returncode
        self.stdin = FakeStdin()
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self.pid = 12345
        self._terminated = False
        self._killed = False

    @property
    def stdout(self):
        """Return an async iterator over lines."""
        return FakeStdout(self._stdout_data)

    async def wait(self):
        self.returncode = self._final_returncode
        return self._final_returncode

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True


class FakeStdout:
    """Async iterator over byte lines."""

    def __init__(self, data: bytes):
        self._lines = data.split(b"\n")
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            if line:
                return line + b"\n"
        raise StopAsyncIteration
