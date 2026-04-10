"""Tests for mdv2 Markdown → Telegram MarkdownV2 conversion."""

import pytest

from boxagent.channels.mdv2 import escape_mdv2, escape_mdv2_code, md_to_mdv2


class TestEscapeMdv2:
    """Test plain-text escaping."""

    def test_no_special_chars(self):
        assert escape_mdv2("hello world") == "hello world"

    def test_chinese_text(self):
        assert escape_mdv2("这是测试文本") == "这是测试文本"

    def test_dot_and_exclamation(self):
        assert escape_mdv2("v1.0.0 is out!") == r"v1\.0\.0 is out\!"

    def test_all_special_chars(self):
        result = escape_mdv2("_ * [ ] ( ) ~ ` > # + - = | { } . ! \\")
        for ch in r"_*[]()~`>#+-=|{}.!\\":
            assert f"\\{ch}" in result or ch == " "

    def test_underscore(self):
        assert escape_mdv2("foo_bar") == r"foo\_bar"

    def test_pipe(self):
        assert escape_mdv2("a | b") == r"a \| b"


class TestEscapeMdv2Code:
    """Test code-context escaping (only ` and \\)."""

    def test_no_special(self):
        assert escape_mdv2_code("hello") == "hello"

    def test_backtick(self):
        assert escape_mdv2_code("use `x`") == r"use \`x\`"

    def test_backslash(self):
        assert escape_mdv2_code(r"path\to\file") == r"path\\to\\file"

    def test_other_chars_untouched(self):
        assert escape_mdv2_code("a_b*c[d]") == "a_b*c[d]"


class TestMdToMdv2PlainText:
    """Test plain text (no markdown structures)."""

    def test_plain_chinese(self):
        assert md_to_mdv2("你好世界") == "你好世界"

    def test_plain_with_dots(self):
        assert md_to_mdv2("Version 1.0.0") == r"Version 1\.0\.0"

    def test_plain_with_exclamation(self):
        assert md_to_mdv2("Hello!") == r"Hello\!"


class TestMdToMdv2InlineCode:
    """Test inline code conversion."""

    def test_simple_inline(self):
        assert md_to_mdv2("use `foo` here") == r"use `foo` here"

    def test_inline_with_special_chars(self):
        result = md_to_mdv2("call `foo.bar()` now")
        assert result == r"call `foo.bar()` now"

    def test_inline_with_underscore(self):
        """Inside inline code, underscore is NOT escaped (only ` and \\)."""
        result = md_to_mdv2("use `foo_bar` here")
        assert result == "use `foo_bar` here"

    def test_inline_code_preserves_special_chars(self):
        """Inside inline code, only ` and \\ are escaped, not other specials."""
        result = md_to_mdv2("run `a*b+c=d`")
        assert "`a*b+c=d`" in result

    def test_inline_backslash(self):
        result = md_to_mdv2(r"path `C:\Users`")
        assert r"`C:\\Users`" in result

    def test_multiple_inline(self):
        result = md_to_mdv2("use `foo` and `bar`")
        assert "`foo`" in result
        assert "`bar`" in result

    def test_inline_at_start(self):
        result = md_to_mdv2("`code` at start")
        assert result.startswith("`code`")

    def test_inline_at_end(self):
        result = md_to_mdv2("at end `code`")
        assert result.endswith("`code`")


class TestMdToMdv2FencedCode:
    """Test fenced code block conversion."""

    def test_simple_code_block(self):
        text = "```\nhello\n```"
        result = md_to_mdv2(text)
        assert result == "```\nhello\n```"

    def test_code_block_with_lang(self):
        text = "```python\nprint('hi')\n```"
        result = md_to_mdv2(text)
        assert result.startswith("```python\n")
        assert result.endswith("```")
        assert "print" in result

    def test_code_block_special_chars_not_escaped(self):
        text = "```\na_b*c[d].e!\n```"
        result = md_to_mdv2(text)
        # Inside code block, only ` and \ are escaped
        assert "a_b*c[d].e!" in result

    def test_code_block_backtick_escaped(self):
        text = "```\nuse `x`\n```"
        result = md_to_mdv2(text)
        assert r"\`" in result

    def test_code_block_backslash_escaped(self):
        text = "```\npath\\to\n```"
        result = md_to_mdv2(text)
        assert r"path\\to" in result

    def test_text_around_code_block(self):
        text = "before.\n```\ncode\n```\nafter!"
        result = md_to_mdv2(text)
        assert r"before\." in result
        assert r"after\!" in result
        assert "```\ncode\n```" in result


class TestMdToMdv2Bold:
    """Test bold conversion: **text** → *text*"""

    def test_simple_bold(self):
        assert md_to_mdv2("**hello**") == "*hello*"

    def test_bold_with_text(self):
        result = md_to_mdv2("say **hello** world")
        assert "*hello*" in result

    def test_bold_inner_special_chars(self):
        result = md_to_mdv2("**v1.0**")
        assert r"*v1\.0*" in result


class TestMdToMdv2Italic:
    """Test italic conversion: *text* → _text_"""

    def test_simple_italic(self):
        assert md_to_mdv2("*hello*") == "_hello_"

    def test_italic_with_text(self):
        result = md_to_mdv2("say *hello* world")
        assert "_hello_" in result


class TestMdToMdv2Strikethrough:
    """Test strikethrough conversion: ~~text~~ → ~text~"""

    def test_simple_strike(self):
        assert md_to_mdv2("~~deleted~~") == "~deleted~"

    def test_strike_with_text(self):
        result = md_to_mdv2("was ~~old~~ now new")
        assert "~old~" in result


class TestMdToMdv2Links:
    """Test link conversion."""

    def test_simple_link(self):
        result = md_to_mdv2("[click](https://example.com)")
        assert "[click](https://example.com)" in result

    def test_link_text_escaped(self):
        result = md_to_mdv2("[v1.0](https://example.com)")
        assert r"[v1\.0]" in result

    def test_link_url_paren_escaped(self):
        result = md_to_mdv2("[x](https://example.com/foo(bar))")
        assert r"foo\(bar\)" in result or r"\)" in result


class TestMdToMdv2Tables:
    """Test markdown table → code block conversion."""

    def test_simple_table(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = md_to_mdv2(text)
        assert result.startswith("```\n")
        assert result.endswith("```")
        # Content should not have mdv2 escaping for |
        assert "\\|" not in result

    def test_table_with_surrounding_text(self):
        text = "header.\n| A | B |\n|---|---|\n| 1 | 2 |\nfooter!"
        result = md_to_mdv2(text)
        assert r"header\." in result
        assert r"footer\!" in result
        assert "```" in result


class TestMdToMdv2Mixed:
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
        result = md_to_mdv2(text)
        # Inline code should be preserved (not escaped inside)
        assert "`noble`" in result
        assert "`x86_64`" in result
        assert "`testuser`" in result
        # Dots in plain text should be escaped
        assert r"24\.04\.4" in result
        # Dashes at line start should be escaped
        assert r"\-" in result

    def test_bold_and_code(self):
        result = md_to_mdv2("**bold** and `code`")
        assert "*bold*" in result
        assert "`code`" in result

    def test_all_formats(self):
        text = "**b** *i* ~~s~~ `c` [link](https://x.com)"
        result = md_to_mdv2(text)
        assert "*b*" in result
        assert "_i_" in result
        assert "~s~" in result
        assert "`c`" in result
        assert "[link](https://x.com)" in result
