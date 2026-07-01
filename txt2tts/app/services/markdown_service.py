"""Markdown -> plain-text conversion tuned for TTS.

Strategy:
  - Parse with markdown-it-py (commonmark profile, tables on).
  - Walk the token stream and emit plain text, skipping markup noise:
      * fenced code blocks  -> dropped entirely
      * code blocks (indented) -> dropped entirely
      * inline code backticks -> stripped
      * images -> alt text (or dropped if empty)
      * links -> label only
      * bold/italic markers -> stripped
      * html -> stripped
  - Keep paragraph breaks as blank lines so TTS has natural pauses.
"""
from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt


_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*\s*$", re.MULTILINE)
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)
_LIST_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_LIST_ORDERED_RE = re.compile(r"^(\s*)\d+\.\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.*?)\1", re.DOTALL)
_EMPHASIS_SINGLE_RE = re.compile(r"(\*|_)(.*?)\1", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


class MarkdownService:
    """Stateless service; safe to share as a singleton."""

    def __init__(self) -> None:
        self._md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(
            "table"
        )

    # -- public API ----------------------------------------------------------

    def read_file(self, path: str | Path) -> str:
        p = Path(path)
        return p.read_text(encoding="utf-8")

    def to_plain_text(self, markdown_text: str) -> str:
        """Convert markdown text to a TTS-friendly plain string."""
        if not markdown_text:
            return ""

        tokens = self._md.parse(markdown_text)

        chunks: list[str] = []
        for tok in tokens:
            ttype = tok.type

            # Drop fenced code blocks and indented code blocks entirely.
            # markdown-it uses type 'fence' for ``` blocks and 'code_block'
            # for indented ones; both contain the raw code in .content.
            if ttype == "fence" or ttype == "code_block":
                continue

            # Inline tokens contain children that we must walk one by one.
            if ttype == "inline":
                if tok.children:
                    chunks.append(self._walk_inline_children(tok.children))
                else:
                    chunks.append(self._clean_inline(tok.content))
                continue

            # Skip all structural open/close markers — they emit no text.
            if ttype in (
                "heading_open", "paragraph_open", "blockquote_open",
                "bullet_list_open", "ordered_list_open", "list_item_open",
                "table_open", "thead_open", "tbody_open", "tr_open",
                "th_open", "td_open", "html_block",
            ):
                continue

            if ttype in (
                "heading_close", "paragraph_close", "blockquote_close",
                "bullet_list_close", "ordered_list_close", "list_item_close",
                "table_close", "thead_close", "tbody_close", "tr_close",
                "th_close", "td_close", "html_inline",
            ):
                chunks.append("\n")
                continue

            if ttype in ("softbreak", "hardbreak"):
                chunks.append("\n")
                continue

            # Fallback: best-effort cleanup.
            if tok.content:
                chunks.append(self._clean_inline(tok.content))

        text = "".join(chunks)

        # Final cleanup pass for residual line-leading markup.
        text = _HEADING_PREFIX_RE.sub("", text)
        text = _BLOCKQUOTE_RE.sub("", text)
        text = _LIST_BULLET_RE.sub(r"\1", text)
        text = _LIST_ORDERED_RE.sub(r"\1", text)
        text = _TABLE_SEP_RE.sub("", text)
        text = _HTML_TAG_RE.sub("", text)
        text = _MULTI_BLANK_RE.sub("\n\n", text)

        return text.strip()

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _walk_inline_children(children: list) -> str:
        """Walk the children of an inline token, handling images and code.

        markdown-it decomposes ![alt](url) into a sequence of:
          text, image (with .content == alt), text
        and `code` into: text, code_inline (with .content == code body), text.
        """
        out: list[str] = []
        for c in children:
            ctype = c.type
            if ctype == "image":
                alt = c.content or ""
                if alt:
                    out.append(alt)
            elif ctype == "code_inline":
                # Strip backticks (the content is already just the code body).
                out.append(c.content or "")
            elif ctype == "softbreak":
                out.append(" ")
            elif ctype == "hardbreak":
                out.append("\n")
            elif ctype == "text":
                out.append(c.content or "")
            else:
                # html_inline / link_open / link_close / em_open / em_close /
                # strong_open / strong_close — link_open/close carry no content,
                # markup markers are stripped by _clean_inline on the joined text.
                if c.content:
                    out.append(MarkdownService._clean_inline(c.content))
        joined = "".join(out)
        return MarkdownService._clean_inline(joined)

    @staticmethod
    def _clean_inline(content: str) -> str:
        """Strip residual inline-level markup from a string."""
        if not content:
            return ""
        s = _IMAGE_RE.sub("", content)
        s = _LINK_RE.sub(r"\1", s)
        s = _INLINE_CODE_RE.sub(r"\1", s)
        s = _EMPHASIS_RE.sub(r"\2", s)
        s = _EMPHASIS_SINGLE_RE.sub(r"\2", s)
        return s