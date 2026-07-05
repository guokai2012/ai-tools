"""本地清洗工具（v6 引入）。

在 ``splitted → ready_to_convert`` 之间插入"本地清洗"步骤：用户通过复选框
选择清洗项（删除 URL / 邮箱 / 代码片段 / 表情符号 / Markdown 符号等），
``apply_local_clean`` 对每个 chunk 跑一遍预编译正则，最后由 TaskManager
覆盖 ``split_<N>.md``。

设计原则：
    - **纯函数**：apply_local_clean 无副作用（不写盘、不读 DB），便于单测；
    - **可叠加**：多个清洗项按 CLEAN_OPTIONS 顺序依次执行；
    - **未启用项 = 原样**：disabled id 完全跳过对应处理函数；
    - **中文友好**：所有正则默认 Unicode 兼容，避免误删汉字。
"""
from __future__ import annotations

import re
from typing import Dict, List


# --------------------------------------------------------------------------
# 清洗项元数据（前端用）
# --------------------------------------------------------------------------


CLEAN_OPTIONS: List[Dict[str, object]] = [
    {
        "id": "url",
        "label": "删除 URL",
        "default": True,
        "description": "删除 http(s):// 开头的链接及其后非空白字符",
    },
    {
        "id": "email",
        "label": "删除邮箱地址",
        "default": True,
        "description": "删除形如 user@domain.tld 的邮箱",
    },
    {
        "id": "code",
        "label": "删除代码片段 / 路径 / 哈希值",
        "default": False,
        "description": "删除围栏代码块、行内代码、常见文件路径、Git SHA-like 哈希",
    },
    {
        "id": "emoji",
        "label": "删除表情符号",
        "default": False,
        "description": "删除 Unicode 表情符号（保留中文标点）",
    },
    {
        "id": "md_symbols",
        "label": "删除 Markdown 语法符号（**、#、*、_、>、|、-）",
        "default": False,
        "description": "删除加粗、标题、斜体、引用、表格等 Markdown 标记",
    },
    {
        "id": "list_marks",
        "label": "删除列表标记（1. 2. - * +）",
        "default": False,
        "description": "删除有序列表（1.）和无序列表（- * +）前缀",
    },
    {
        "id": "blockquote",
        "label": "删除引用前缀 >",
        "default": False,
        "description": "删除每行开头的 > 引用标记（与 md_symbols 部分重叠；启用本项即使未勾选 md_symbols 也生效）",
    },
    {
        "id": "table_pipe",
        "label": "删除表格分隔符 |",
        "default": False,
        "description": "删除 Markdown 表格的 | 列分隔符",
    },
]


def get_clean_options() -> List[Dict[str, object]]:
    """返回清洗项元数据列表（深拷贝，避免外部修改 CLEAN_OPTIONS）。"""
    return [dict(opt) for opt in CLEAN_OPTIONS]


def get_default_clean_ids() -> List[str]:
    """返回默认勾选的清洗项 id（用于新草稿/重置）。"""
    return [opt["id"] for opt in CLEAN_OPTIONS if opt.get("default")]


# --------------------------------------------------------------------------
# 正则模式（预编译，模块级缓存）
# --------------------------------------------------------------------------


# 1. URL: http(s):// 后跟非空白字符；尾部标点保留
_RE_URL = re.compile(r"https?://\S+")

# 2. 邮箱
_RE_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b")

# 3a. 围栏代码块 ```...```
_RE_FENCE = re.compile(r"```[\s\S]*?```")

# 3b. 行内代码 `xxx`
_RE_INLINE_CODE = re.compile(r"`[^`\n]+`")

# 3c. 常见文件路径（可选 ./ 或 ../ 前缀）：含 .ext 后缀
_RE_FILE_PATH = re.compile(
    r"(?:\.{0,2}/)?[\w./\-]+\.(?:py|js|jsx|ts|tsx|md|markdown|json|ya?ml|toml|sh|bash|zsh|go|rs|java|kt|cpp|cc|cxx|h|hpp|cs|rb|php|html|htm|css|scss|sass|sql|txt|log|csv|tsv|xml|vue|svelte)\b",
    re.IGNORECASE,
)

# 3d. Git SHA-like 哈希：7~40 位十六进制
_RE_SHA = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)

# 4. Emoji Unicode 范围（覆盖大部分常用 emoji；不含中文标点）
_RE_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs / emoticons / etc.
    "\U00002600-\U000027BF"   # miscellaneous symbols + dingbats
    "\U0001F600-\U0001F64F"   # emoticons face
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-A
    "\U00002B00-\U00002BFF"   # arrows
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "]+",
)

# 5a. Markdown 加粗 **xxx** / __xxx__
_RE_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
# 5b. Markdown 标题前缀 # ## ### （行首）
_RE_MD_HEADING = re.compile(r"(?m)^#{1,6}\s+")
# 5c. Markdown 斜体 *xxx* 或 _xxx_（不匹配 **）
_RE_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
# 5d. Markdown 删除线 ~~xxx~~
_RE_MD_STRIKE = re.compile(r"~~(.+?)~~")

# 6. 列表标记（行首）
_RE_LIST_ORDERED = re.compile(r"(?m)^\s*\d+\.\s+")
_RE_LIST_UNORDERED = re.compile(r"(?m)^\s*[-*+]\s+")

# 7. 引用前缀 > （行首，可多个 + 空格）
_RE_BLOCKQUOTE = re.compile(r"(?m)^\s*>+\s?")

# 8. 表格分隔符 | （行内）；保留单元格内容
_RE_TABLE_PIPE = re.compile(r"(?m)^\s*\|?(.+?)\|?\s*$")
_RE_TABLE_SEP = re.compile(r"(?m)^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


# --------------------------------------------------------------------------
# 单项清洗函数
# --------------------------------------------------------------------------


def _clean_url(text: str) -> str:
    """删除 URL；URL 后的多余空白压缩成 1 个空格。"""
    cleaned = _RE_URL.sub("", text)
    # 清理 URL 留下的孤立空格（如 "详情见  。"）
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def _clean_email(text: str) -> str:
    return _RE_EMAIL.sub("", text)


def _clean_code(text: str) -> str:
    """删除代码块 → 行内代码 → 文件路径 → Git SHA。"""
    text = _RE_FENCE.sub("", text)
    text = _RE_INLINE_CODE.sub("", text)
    text = _RE_FILE_PATH.sub("", text)
    text = _RE_SHA.sub("", text)
    return text


def _clean_emoji(text: str) -> str:
    return _RE_EMOJI.sub("", text)


def _clean_md_symbols(text: str) -> str:
    """删除 Markdown 标记符号（** # * _ ~~）。"""
    text = _RE_MD_BOLD.sub(r"\1\2", text)  # 保留内容，去 **
    text = _RE_MD_STRIKE.sub(r"\1", text)  # 保留内容，去 ~~
    text = _RE_MD_HEADING.sub("", text)    # 标题前缀去掉
    text = _RE_MD_ITALIC.sub(r"\1\2", text)  # 保留内容，去 * / _
    return text


def _clean_list_marks(text: str) -> str:
    text = _RE_LIST_ORDERED.sub("", text)
    text = _RE_LIST_UNORDERED.sub("", text)
    return text


def _clean_blockquote(text: str) -> str:
    return _RE_BLOCKQUOTE.sub("", text)


def _clean_table_pipe(text: str) -> str:
    """删除 Markdown 表格的 | 分隔符；保留单元格内容。"""

    def _strip_pipe_line(m: re.Match) -> str:
        line = m.group(1)
        return line.strip()

    text = _RE_TABLE_PIPE.sub(_strip_pipe_line, text)
    # 表格分隔行 |---|---|
    text = _RE_TABLE_SEP.sub("", text)
    return text


# id → 处理函数映射
_CLEANERS: Dict[str, callable] = {
    "url": _clean_url,
    "email": _clean_email,
    "code": _clean_code,
    "emoji": _clean_emoji,
    "md_symbols": _clean_md_symbols,
    "list_marks": _clean_list_marks,
    "blockquote": _clean_blockquote,
    "table_pipe": _clean_table_pipe,
}


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def apply_local_clean(text: str, enabled_ids: List[str]) -> str:
    """对 ``text`` 应用 ``enabled_ids`` 中的所有清洗项；未在表中的 id 静默跳过。

    按 CLEAN_OPTIONS 声明顺序执行，便于稳定输出（先 URL/邮箱 再 Markdown）。
    """
    if not text:
        return text
    if not enabled_ids:
        return text

    # 去重 + 保序
    seen: set = set()
    ordered: List[str] = []
    for opt in CLEAN_OPTIONS:
        cid = opt["id"]  # type: ignore[index]
        if cid in enabled_ids and cid not in seen:
            ordered.append(cid)  # type: ignore[arg-type]
            seen.add(cid)

    for cid in ordered:  # type: ignore[assignment]
        cleaner = _CLEANERS.get(cid)
        if cleaner is None:
            continue
        text = cleaner(text)
    return text


def clean_summary(before: str, after: str) -> Dict[str, int]:
    """清洗前后对比摘要（前端展示用）。"""
    return {
        "before_chars": len(before),
        "after_chars": len(after),
        "removed_chars": max(0, len(before) - len(after)),
    }