"""Manual E2E test harness for BoxAgent backends.

These tests hit real LLMs. They're NOT auto-asserted — they print logs
for a human or AI to read and judge. Run via:

    uv run python -m manual_e2e.run --backend claude-cli --scenario hello
"""
