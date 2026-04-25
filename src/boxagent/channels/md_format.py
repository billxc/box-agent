"""Convert standard Markdown to platform-specific formats.

- Telegram MarkdownV2: escape special chars, remap bold/italic/strike syntax.
- Discord: convert unsupported elements (tables, links) to Discord-friendly forms.
"""

import re

# ── Telegram MarkdownV2 ─────────────────────────────────────────────

# Chars that must be escaped in MDv2 plain text.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"

# Single-pass tokenizer: match structures that need special handling.
_TOKEN_RE = re.compile(
    r"(?P<fence>```(\w*)\n?[\s\S]*?```)"   # fenced code block
    r"|(?P<table>(?:^|\n)\|[^\n]+\|\n\|[-| :]+\|(?:\n\|[^\n]+\|)*)"  # markdown table
    r"|(?P<inline>`[^`\n]+`)"               # inline code
    r"|(?P<bold>\*\*(?P<bold_inner>.+?)\*\*)"  # **bold**
    r"|(?P<italic>(?<!\w)\*(?!\s)(?P<italic_inner>.+?)(?<!\s)\*(?!\w))"  # *italic*
    r"|(?P<strike>~~(?P<strike_inner>.+?)~~)"  # ~~strikethrough~~
    r"|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)]+)\))"  # [text](url)
)


def escape_telegram(text: str) -> str:
    """Escape special chars for MarkdownV2 plain text."""
    return re.sub(r"([" + re.escape(_MDV2_SPECIAL) + r"])", r"\\\1", text)


def escape_telegram_code(text: str) -> str:
    """Escape only ` and \\ inside code/pre entities."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def md_to_telegram(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2.

    Single-pass tokenizer approach:
    - Code blocks/inline code: escape only ` and \\
    - Bold **x** → *x*, italic *x* → _x_, strike ~~x~~ → ~x~
    - Links [t](u): escape text, escape ) and \\ in URL
    - Everything else: escape all 20 special chars
    """
    result: list[str] = []
    pos = 0

    for m in _TOKEN_RE.finditer(text):
        # Escape plain text between tokens
        result.append(escape_telegram(text[pos:m.start()]))

        if m.group("fence"):
            # Fenced code block: ``` kept as-is, content escape ` and \
            lang = m.group(2) or ""
            raw = m.group("fence")
            # Extract content between opening ``` and closing ```
            if lang:
                content = raw[3 + len(lang):].lstrip("\n").removesuffix("```")
            else:
                content = raw[3:].lstrip("\n").removesuffix("```")
            escaped = escape_telegram_code(content)
            result.append(f"```{lang}\n{escaped}```")
        elif m.group("table"):
            # Markdown table → wrap in code block
            table = m.group("table").strip()
            escaped = escape_telegram_code(table)
            result.append(f"```\n{escaped}```")
        elif m.group("inline"):
            # Inline code: extract content, escape ` and \
            raw = m.group("inline")
            content = raw[1:-1]
            result.append(f"`{escape_telegram_code(content)}`")
        elif m.group("bold"):
            result.append(f"*{escape_telegram(m.group('bold_inner'))}*")
        elif m.group("italic"):
            result.append(f"_{escape_telegram(m.group('italic_inner'))}_")
        elif m.group("strike"):
            result.append(f"~{escape_telegram(m.group('strike_inner'))}~")
        elif m.group("link"):
            link_text = escape_telegram(m.group("link_text"))
            link_url = m.group("link_url").replace("\\", "\\\\").replace(")", "\\)")
            result.append(f"[{link_text}]({link_url})")

        pos = m.end()

    # Escape remaining plain text
    result.append(escape_telegram(text[pos:]))
    return "".join(result)


# ── Discord ──────────────────────────────────────────────────────────

# Tokenizer for Discord: only match elements that need transformation.
# Code fences are matched first to protect them from other transforms.
_DISCORD_TOKEN_RE = re.compile(
    r"(?P<fence>```(\w*)\n?[\s\S]*?```)"   # fenced code block (preserve)
    r"|(?P<table>(?:^|\n)\|[^\n]+\|\n\|[-| :]+\|(?:\n\|[^\n]+\|)*)"  # markdown table
    r"|(?P<inline>`[^`\n]+`)"               # inline code (preserve)
)


def md_to_discord(text: str) -> str:
    """Convert standard Markdown to Discord-friendly format.

    Discord natively supports: **bold**, *italic*, ~~strike~~, `code`,
    ```code blocks```, > quotes, lists, [text](url) links.

    Transforms needed:
    - Tables → wrapped in code block for alignment
    """
    result: list[str] = []
    pos = 0

    for m in _DISCORD_TOKEN_RE.finditer(text):
        # Plain text between tokens: pass through as-is
        result.append(text[pos:m.start()])

        if m.group("fence"):
            # Code blocks: pass through unchanged
            result.append(m.group("fence"))
        elif m.group("table"):
            # Markdown table → code block
            table = m.group("table").strip()
            result.append(f"```\n{table}\n```")
        elif m.group("inline"):
            # Inline code: pass through unchanged
            result.append(m.group("inline"))

        pos = m.end()

    # Remaining plain text
    result.append(text[pos:])
    return "".join(result)


# ── Backward compatibility aliases ───────────────────────────────────

escape_mdv2 = escape_telegram
escape_mdv2_code = escape_telegram_code
md_to_mdv2 = md_to_telegram
