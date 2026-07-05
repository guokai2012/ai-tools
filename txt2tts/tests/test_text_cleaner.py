"""v6 本地清洗工具测试。

针对 ``app/services/text_cleaner.py`` 中 8 个清洗项的独立 / 叠加 / 边界场景。
"""
from __future__ import annotations

import pytest

from app.services.text_cleaner import (
    CLEAN_OPTIONS,
    apply_local_clean,
    clean_summary,
    get_clean_options,
    get_default_clean_ids,
)


# ---------- 元数据 ----------


class TestCleanOptionsMetadata:
    def test_get_clean_options_returns_all_eight(self):
        opts = get_clean_options()
        assert len(opts) == 8

    def test_clean_options_have_required_fields(self):
        opts = get_clean_options()
        for opt in opts:
            assert "id" in opt and "label" in opt
            assert isinstance(opt["id"], str)
            assert isinstance(opt["label"], str)
            assert isinstance(opt.get("default", False), bool)

    def test_get_clean_options_returns_deep_copy(self):
        a = get_clean_options()
        a[0]["label"] = "MUTATED"
        b = get_clean_options()
        assert b[0]["label"] != "MUTATED"

    def test_default_clean_ids_contains_url_and_email(self):
        defaults = get_default_clean_ids()
        assert "url" in defaults
        assert "email" in defaults

    def test_known_ids(self):
        ids = {opt["id"] for opt in CLEAN_OPTIONS}
        assert ids == {
            "url", "email", "code", "emoji",
            "md_symbols", "list_marks", "blockquote", "table_pipe",
        }


# ---------- 单项清洗 ----------


class TestCleanUrl:
    def test_removes_http_url(self):
        out = apply_local_clean("详情见 https://example.com/foo", ["url"])
        assert "https://example.com/foo" not in out
        assert "详情见" in out

    def test_removes_https_url_with_path_and_query(self):
        out = apply_local_clean("链接 https://a.b/c?d=1&e=2 结束", ["url"])
        assert "http" not in out

    def test_no_url_passes_through(self):
        out = apply_local_clean("纯中文内容", ["url"])
        assert out == "纯中文内容"


class TestCleanEmail:
    def test_removes_email(self):
        out = apply_local_clean("联系我 user@example.com", ["email"])
        assert "user@example.com" not in out

    def test_preserves_at_sign_in_text(self):
        out = apply_local_clean("C++ 标准 @ 符号", ["email"])
        assert "@ 符号" in out  # 没有 .tld 不算邮箱


class TestCleanCode:
    def test_removes_fenced_code_block(self):
        text = "前面\n```python\ndef foo(): pass\n```\n后面"
        out = apply_local_clean(text, ["code"])
        assert "def foo" not in out
        assert "前面" in out and "后面" in out

    def test_removes_inline_code(self):
        out = apply_local_clean("使用 `pip install` 安装", ["code"])
        assert "pip install" not in out
        assert "使用" in out

    def test_removes_file_path(self):
        out = apply_local_clean("运行 ./scripts/build.py 即可", ["code"])
        assert "scripts/build.py" not in out

    def test_removes_sha_like_hash(self):
        out = apply_local_clean("commit 1a2b3c4d5e6f7890 修复", ["code"])
        assert "1a2b3c4d5e6f7890" not in out


class TestCleanEmoji:
    def test_removes_emoji(self):
        out = apply_local_clean("完成 🎉 庆祝", ["emoji"])
        assert "🎉" not in out

    def test_preserves_chinese(self):
        out = apply_local_clean("你好世界", ["emoji"])
        assert out == "你好世界"


class TestCleanMarkdownSymbols:
    def test_removes_bold(self):
        out = apply_local_clean("这是 **重要** 的", ["md_symbols"])
        assert "重要" in out
        assert "**" not in out

    def test_removes_heading_prefix(self):
        out = apply_local_clean("# 标题\n## 副标题", ["md_symbols"])
        assert "标题" in out and "副标题" in out
        assert "#" not in out

    def test_removes_italic(self):
        out = apply_local_clean("这是 *斜体*", ["md_symbols"])
        assert "斜体" in out
        assert "*斜体*" not in out


class TestCleanListMarks:
    def test_removes_ordered_list(self):
        out = apply_local_clean("1. 第一\n2. 第二", ["list_marks"])
        assert "第一" in out and "第二" in out
        assert "1." not in out and "2." not in out

    def test_removes_unordered_list(self):
        out = apply_local_clean("- 苹果\n* 香蕉\n+ 橘子", ["list_marks"])
        for fruit in ("苹果", "香蕉", "橘子"):
            assert fruit in out
        assert "- " not in out and "* " not in out and "+ " not in out


class TestCleanBlockquote:
    def test_removes_blockquote_prefix(self):
        out = apply_local_clean("> 引言\n> 多行", ["blockquote"])
        assert "引言" in out and "多行" in out
        assert "> " not in out


class TestCleanTablePipe:
    def test_removes_table_separator(self):
        text = "| col1 | col2 |\n| --- | --- |\n| a | b |"
        out = apply_local_clean(text, ["table_pipe"])
        assert "---" not in out
        assert "col1" in out and "a" in out


# ---------- 多项叠加 / 边界 ----------


class TestCombinedAndEdge:
    def test_empty_text_passthrough(self):
        assert apply_local_clean("", ["url"]) == ""
        assert apply_local_clean("hello", []) == "hello"

    def test_empty_enabled_ids_passthrough(self):
        out = apply_local_clean("有 https://x.com 链接", [])
        assert "https://x.com" in out

    def test_unknown_id_silently_skipped(self):
        out = apply_local_clean("hello https://x.com", ["url", "bogus_id"])
        assert "https://x.com" not in out

    def test_combined_url_email_md_symbols(self):
        text = (
            "详情 https://example.com 联系 **user@test.com** 邮箱\n"
            "# 标题"
        )
        out = apply_local_clean(text, ["url", "email", "md_symbols"])
        assert "https://example.com" not in out
        assert "user@test.com" not in out
        assert "#" not in out
        assert "详情" in out and "联系" in out and "标题" in out

    def test_clean_summary(self):
        s = clean_summary("hello world", "hello")
        assert s["before_chars"] == 11
        assert s["after_chars"] == 5
        assert s["removed_chars"] == 6

    def test_chinese_passthrough_safe(self):
        # 没有任何清洗项触发时中文不应被破坏
        text = "床前明月光，疑是地上霜。举头望明月，低头思故乡。"
        out = apply_local_clean(text, ["url"])
        assert out == text

    def test_no_partial_word_damage_in_email(self):
        # 邮箱前的数字 ID 不应被误判为哈希
        out = apply_local_clean("ID 1234 user@test.com 邮件", ["code"])
        assert "user@test.com" in out  # email 不在 code 项内