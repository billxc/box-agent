"""BoxAgent test doubles.

Mock implementations of the Protocols defined in ``boxagent.agent`` and
``boxagent.transports``. Use these from ``tests/`` instead of hand-rolling
``MagicMock`` / ``AsyncMock``; they record calls and let tests script
behaviour without re-discovering the interface every time.
"""
