"""Unit tests for MarkdownService. Run with:
    D:\\anaconda3\\python.exe -m pytest tests/ -v
"""
from pathlib import Path

from app.services.markdown_service import MarkdownService

SAMPLE = """# 标题

这是一段正文。

## 子标题

- 列表项 1
- 列表项 2

[一个链接](https://example.com)

```python
def code_block():
    pass
```

> 一段引用

行内 `code` 也会被处理。
"""


def test_strips_headings_and_emphasis():
    md = MarkdownService()
    out = md.to_plain_text(SAMPLE)
    assert "# 标题" not in out
    assert "```" not in out
    assert "https://example.com" not in out
    assert "一个链接" in out
    assert "列表项 1" in out
    assert "def code_block" not in out
    assert "一段引用" in out
    assert "code 也会被处理" in out


def test_empty_input():
    md = MarkdownService()
    assert md.to_plain_text("") == ""
    assert md.to_plain_text("   \n\n  ") == ""


def test_image_dropped_or_alt_kept():
    md = MarkdownService()
    out = md.to_plain_text("看这张图：![美丽的风景](https://img.example.com/a.png) 很好看")
    assert "https://img.example.com" not in out
    # alt text is preserved
    assert "美丽的风景" in out


def test_table_separator_removed():
    md = MarkdownService()
    src = """| col1 | col2 |
| --- | --- |
| a   | b   |"""
    out = md.to_plain_text(src)
    assert "---" not in out
    assert "col1" in out and "a" in out