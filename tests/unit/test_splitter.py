"""Tests for Telegram message splitting logic."""

from boxagent.channels.splitter import split_message

TELEGRAM_LIMIT = 4096


class TestSplitMessage:
    def test_short_message_no_split(self):
        """Message under limit returned as single chunk."""
        text = "Hello world"
        chunks = split_message(text, TELEGRAM_LIMIT)
        assert chunks == ["Hello world"]

    def test_long_message_splits_at_paragraph(self):
        """Long message splits at paragraph boundary (double newline)."""
        para1 = "A" * 2000
        para2 = "B" * 2000
        para3 = "C" * 2000
        text = f"{para1}\n\n{para2}\n\n{para3}"
        chunks = split_message(text, TELEGRAM_LIMIT)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= TELEGRAM_LIMIT

    def test_code_block_not_split(self):
        """Never split inside a code fence."""
        code = "```python\n" + "x = 1\n" * 500 + "```"
        prefix = "A" * 3000 + "\n\n"
        text = prefix + code
        chunks = split_message(text, TELEGRAM_LIMIT)
        # Every chunk with a code fence must have an even number of fences
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, (
                f"Chunk has odd number of code fences ({fence_count}): "
                f"code block was split"
            )

    def test_single_line_exceeding_limit(self):
        """A single line longer than limit is force-split at limit."""
        text = "A" * 5000
        chunks = split_message(text, TELEGRAM_LIMIT)
        assert len(chunks) == 2
        assert len(chunks[0]) <= TELEGRAM_LIMIT
        assert chunks[0] + chunks[1] == text

    def test_empty_message(self):
        """Empty message returns empty list."""
        assert split_message("", TELEGRAM_LIMIT) == []

    def test_preserves_full_content(self):
        """All original paragraph content is present across chunks."""
        para1 = "A" * 2000
        para2 = "B" * 2000
        para3 = "C" * 500
        text = f"{para1}\n\n{para2}\n\n{para3}"
        chunks = split_message(text, TELEGRAM_LIMIT)
        rejoined = "\n\n".join(chunks)
        assert para1 in rejoined
        assert para2 in rejoined
        assert para3 in rejoined
