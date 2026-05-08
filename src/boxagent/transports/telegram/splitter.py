"""Split long messages at safe boundaries for Telegram (4096 char limit)."""


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into chunks that fit within `limit` characters.

    Split strategy (priority order):
    1. Paragraph boundary (double newline)
    2. Single newline
    3. Force split at limit (last resort)

    Never splits inside a code fence (``` ... ```).
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, limit)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


def _find_split_point(text: str, limit: int) -> int:
    """Find the best position to split text at, respecting code blocks."""
    candidate = text[:limit]

    # Check if we're inside a code block at the limit boundary
    fence_count = candidate.count("```")
    if fence_count % 2 == 1:
        last_fence = candidate.rfind("```")
        if last_fence > 0:
            before_fence = text[:last_fence].rstrip()
            if before_fence:
                return len(before_fence)

    # Try paragraph boundary (double newline)
    para_break = candidate.rfind("\n\n")
    if para_break > limit // 4:
        return para_break

    # Try single newline
    line_break = candidate.rfind("\n")
    if line_break > limit // 4:
        return line_break

    # Force split at limit
    return limit
