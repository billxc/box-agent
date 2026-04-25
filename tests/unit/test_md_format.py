"""Tests for md_format — Markdown conversion for Telegram and Discord."""

import pytest

from boxagent.channels.md_format import (
    escape_telegram,
    escape_telegram_code,
    md_to_telegram,
    md_to_discord,
)


# ── Telegram MarkdownV2 tests ────────────────────────────────────────


class TestEscapeTelegram:
    """Test plain-text escaping."""

    def test_no_special_chars(self):
        assert escape_telegram("hello world") == "hello world"

    def test_chinese_text(self):
        assert escape_telegram("这是测试文本") == "这是测试文本"

    def test_dot_and_exclamation(self):
        assert escape_telegram("v1.0.0 is out!") == r"v1\.0\.0 is out\!"

    def test_all_special_chars(self):
        result = escape_telegram("_ * [ ] ( ) ~ ` > # + - = | { } . ! \\")
        for ch in r"_*[]()~`>#+-=|{}.!\\":
            assert f"\\{ch}" in result or ch == " "

    def test_underscore(self):
        assert escape_telegram("foo_bar") == r"foo\_bar"

    def test_pipe(self):
        assert escape_telegram("a | b") == r"a \| b"


class TestEscapeTelegramCode:
    """Test code-context escaping (only ` and \\)."""

    def test_no_special(self):
        assert escape_telegram_code("hello") == "hello"

    def test_backtick(self):
        assert escape_telegram_code("use `x`") == r"use \`x\`"

    def test_backslash(self):
        assert escape_telegram_code(r"path\to\file") == r"path\\to\\file"

    def test_other_chars_untouched(self):
        assert escape_telegram_code("a_b*c[d]") == "a_b*c[d]"


class TestMdToTelegramPlainText:
    """Test plain text (no markdown structures)."""

    def test_plain_chinese(self):
        assert md_to_telegram("你好世界") == "你好世界"

    def test_plain_with_dots(self):
        assert md_to_telegram("Version 1.0.0") == r"Version 1\.0\.0"

    def test_plain_with_exclamation(self):
        assert md_to_telegram("Hello!") == r"Hello\!"


class TestMdToTelegramInlineCode:
    """Test inline code conversion."""

    def test_simple_inline(self):
        assert md_to_telegram("use `foo` here") == r"use `foo` here"

    def test_inline_with_special_chars(self):
        result = md_to_telegram("call `foo.bar()` now")
        assert result == r"call `foo.bar()` now"

    def test_inline_with_underscore(self):
        """Inside inline code, underscore is NOT escaped (only ` and \\)."""
        result = md_to_telegram("use `foo_bar` here")
        assert result == "use `foo_bar` here"

    def test_inline_code_preserves_special_chars(self):
        """Inside inline code, only ` and \\ are escaped, not other specials."""
        result = md_to_telegram("run `a*b+c=d`")
        assert "`a*b+c=d`" in result

    def test_inline_backslash(self):
        result = md_to_telegram(r"path `C:\Users`")
        assert r"`C:\\Users`" in result

    def test_multiple_inline(self):
        result = md_to_telegram("use `foo` and `bar`")
        assert "`foo`" in result
        assert "`bar`" in result

    def test_inline_at_start(self):
        result = md_to_telegram("`code` at start")
        assert result.startswith("`code`")

    def test_inline_at_end(self):
        result = md_to_telegram("at end `code`")
        assert result.endswith("`code`")


class TestMdToTelegramFencedCode:
    """Test fenced code block conversion."""

    def test_simple_code_block(self):
        text = "```\nhello\n```"
        result = md_to_telegram(text)
        assert result == "```\nhello\n```"

    def test_code_block_with_lang(self):
        text = "```python\nprint('hi')\n```"
        result = md_to_telegram(text)
        assert result.startswith("```python\n")
        assert result.endswith("```")
        assert "print" in result

    def test_code_block_special_chars_not_escaped(self):
        text = "```\na_b*c[d].e!\n```"
        result = md_to_telegram(text)
        # Inside code block, only ` and \ are escaped
        assert "a_b*c[d].e!" in result

    def test_code_block_backtick_escaped(self):
        text = "```\nuse `x`\n```"
        result = md_to_telegram(text)
        assert r"\`" in result

    def test_code_block_backslash_escaped(self):
        text = "```\npath\\to\n```"
        result = md_to_telegram(text)
        assert r"path\\to" in result

    def test_text_around_code_block(self):
        text = "before.\n```\ncode\n```\nafter!"
        result = md_to_telegram(text)
        assert r"before\." in result
        assert r"after\!" in result
        assert "```\ncode\n```" in result


class TestMdToTelegramBold:
    """Test bold conversion: **text** → *text*"""

    def test_simple_bold(self):
        assert md_to_telegram("**hello**") == "*hello*"

    def test_bold_with_text(self):
        result = md_to_telegram("say **hello** world")
        assert "*hello*" in result

    def test_bold_inner_special_chars(self):
        result = md_to_telegram("**v1.0**")
        assert r"*v1\.0*" in result


class TestMdToTelegramItalic:
    """Test italic conversion: *text* → _text_"""

    def test_simple_italic(self):
        assert md_to_telegram("*hello*") == "_hello_"

    def test_italic_with_text(self):
        result = md_to_telegram("say *hello* world")
        assert "_hello_" in result


class TestMdToTelegramStrikethrough:
    """Test strikethrough conversion: ~~text~~ → ~text~"""

    def test_simple_strike(self):
        assert md_to_telegram("~~deleted~~") == "~deleted~"

    def test_strike_with_text(self):
        result = md_to_telegram("was ~~old~~ now new")
        assert "~old~" in result


class TestMdToTelegramLinks:
    """Test link conversion."""

    def test_simple_link(self):
        result = md_to_telegram("[click](https://example.com)")
        assert "[click](https://example.com)" in result

    def test_link_text_escaped(self):
        result = md_to_telegram("[v1.0](https://example.com)")
        assert r"[v1\.0]" in result

    def test_link_url_paren_escaped(self):
        result = md_to_telegram("[x](https://example.com/foo(bar))")
        assert r"foo\(bar\)" in result or r"\)" in result


class TestMdToTelegramTables:
    """Test markdown table → code block conversion."""

    def test_simple_table(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = md_to_telegram(text)
        assert result.startswith("```\n")
        assert result.endswith("```")
        # Content should not have mdv2 escaping for |
        assert "\\|" not in result

    def test_table_with_surrounding_text(self):
        text = "header.\n| A | B |\n|---|---|\n| 1 | 2 |\nfooter!"
        result = md_to_telegram(text)
        assert r"header\." in result
        assert r"footer\!" in result
        assert "```" in result


class TestMdToTelegramMixed:
    """Test real-world mixed content."""

    def test_system_info(self):
        """The actual message that triggered the bug report."""
        text = (
            "系统信息如下：\n\n"
            "- 系统：Ubuntu 24.04.4 LTS\n"
            "- 代号：`noble`\n"
            "- 内核：`6.6.87.2-microsoft-standard-WSL2`\n"
            "- 架构：`x86_64`\n"
            "- 主机名：`DESKTOP-ABCDEFG`\n"
            "- 当前用户：`testuser`\n"
            "- 当前时间：`2026-03-25 17:37:31 UTC`"
        )
        result = md_to_telegram(text)
        # Inline code should be preserved (not escaped inside)
        assert "`noble`" in result
        assert "`x86_64`" in result
        assert "`testuser`" in result
        # Dots in plain text should be escaped
        assert r"24\.04\.4" in result
        # Dashes at line start should be escaped
        assert r"\-" in result

    def test_bold_and_code(self):
        result = md_to_telegram("**bold** and `code`")
        assert "*bold*" in result
        assert "`code`" in result

    def test_all_formats(self):
        text = "**b** *i* ~~s~~ `c` [link](https://x.com)"
        result = md_to_telegram(text)
        assert "*b*" in result
        assert "_i_" in result
        assert "~s~" in result
        assert "`c`" in result
        assert "[link](https://x.com)" in result


# ── Discord tests ────────────────────────────────────────────────────


class TestMdToDiscordPassthrough:
    """Discord natively supports these — should pass through unchanged."""

    def test_plain_text(self):
        assert md_to_discord("hello world") == "hello world"

    def test_bold(self):
        assert md_to_discord("**bold**") == "**bold**"

    def test_italic(self):
        assert md_to_discord("*italic*") == "*italic*"

    def test_strikethrough(self):
        assert md_to_discord("~~deleted~~") == "~~deleted~~"

    def test_inline_code(self):
        assert md_to_discord("use `foo` here") == "use `foo` here"

    def test_code_block(self):
        text = "```python\nprint('hi')\n```"
        assert md_to_discord(text) == text

    def test_code_block_preserved(self):
        text = "before\n```\ncode\n```\nafter"
        assert md_to_discord(text) == text


class TestMdToDiscordTables:
    """Tables are not supported in Discord — convert to code blocks."""

    def test_simple_table(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = md_to_discord(text)
        assert result.startswith("```\n")
        assert result.endswith("\n```")
        assert "| A | B |" in result
        assert "| 1 | 2 |" in result

    def test_table_with_surrounding_text(self):
        text = "Results:\n| Name | Score |\n|------|-------|\n| Alice | 95 |\nDone!"
        result = md_to_discord(text)
        assert "Results:" in result
        assert "Done!" in result
        assert "```\n" in result

    def test_table_alignment(self):
        """Table content inside code block should preserve alignment."""
        text = "| Left | Right |\n|:-----|------:|\n| a    |     b |"
        result = md_to_discord(text)
        assert "| Left | Right |" in result
        assert "|:-----|------:|" in result


class TestMdToDiscordLinks:
    """Masked links are natively supported in Discord — pass through."""

    def test_simple_link(self):
        result = md_to_discord("[click here](https://example.com)")
        assert result == "[click here](https://example.com)"

    def test_link_with_surrounding_text(self):
        result = md_to_discord("Visit [docs](https://docs.example.com) for more.")
        assert result == "Visit [docs](https://docs.example.com) for more."

    def test_link_inside_code_block_untouched(self):
        """Links inside code blocks should not be transformed."""
        text = "```\n[link](https://example.com)\n```"
        assert md_to_discord(text) == text


class TestMdToDiscordMixed:
    """Real-world mixed content."""

    def test_bold_code_and_table(self):
        text = (
            "**Summary**\n\n"
            "Use `cmd` to run:\n\n"
            "| Flag | Desc |\n|------|------|\n| -v | verbose |\n\n"
            "See [docs](https://example.com)."
        )
        result = md_to_discord(text)
        assert "**Summary**" in result
        assert "`cmd`" in result
        assert "```\n" in result
        assert "docs (<https://example.com>)" not in result
        assert "[docs](https://example.com)" in result

    def test_all_native_formats_preserved(self):
        text = "**b** *i* ~~s~~ `c` > quote"
        assert md_to_discord(text) == text
