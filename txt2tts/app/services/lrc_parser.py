"""LRC 歌词文件解析器（纯函数，供 pytest 覆盖算法正确性）。

前端 ``app/static/app.js`` 里有等价的 JavaScript 实现，
二者必须保持行为一致。修改本文件时同步修改 JS 实现。

支持的格式：
    * 标准 ``[mm:ss.xx]``、``[mm:ss]``、增强毫秒 ``[mm:ss.xxx]``
    * 一行多个时间戳 ``[00:01.00][00:05.00]歌词`` → 展开成两条
    * 元信息行 ``[ti:标题]`` ``[ar:作者]`` ``[al:专辑]`` ``[by:]`` ``[offset:0]`` → 跳过
    * 空行 / 无时间戳行 → 跳过
    * 同一时间的连续多行共享同一 ``lineIdx``，使播放时同步高亮

返回值：
    ``List[dict]``，每条形如 ``{"time": 1.5, "text": "...", "lineIdx": 0}``；
    解析失败（无任何时间戳行）返回空列表。
"""
from __future__ import annotations

import re
from typing import List, Dict, Optional, Tuple


_TS_RE = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_LINE_RE = re.compile(r"^\s*\[[a-zA-Z]+:")


def _parse_timestamp(minutes: str, seconds: str, frac: Optional[str]) -> float:
    mm = int(minutes)
    ss = int(seconds)
    if frac:
        # 兼容 [00:01.5]、[00:01.50]、[00:01.500]；统一补到 3 位后取前 3 位
        frac_padded = (frac + "000")[:3]
        ms = int(frac_padded)
    else:
        ms = 0
    return mm * 60 + ss + ms / 1000.0


def parse_lrc(text: str) -> List[Dict]:
    """把 LRC 文本解析为 ``[{time, text, lineIdx}, ...]``。

    解析失败或输入为空时返回空列表。
    """
    if not text or not isinstance(text, str):
        return []
    out: List[Dict] = []
    line_idx = -1
    last_sig_ts = -1.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        matches = list(_TS_RE.finditer(line))
        if not matches:
            continue
        # 提取剩余文本（去掉所有时间戳标记）
        text_part = _TS_RE.sub("", line).strip()
        # 跳过纯元信息行（无可见歌词文本且以 [meta: 开头）
        if not text_part and _META_LINE_RE.match(line):
            continue
        for m in matches:
            t = _parse_timestamp(m.group(1), m.group(2), m.group(3))
            if t != last_sig_ts:
                line_idx += 1
                last_sig_ts = t
            out.append({"time": t, "text": text_part or "", "lineIdx": line_idx})
    return out


def find_current_lrc_idx(entries: List[Dict], t: float) -> int:
    """二分查找：返回 currentTime 时刻对应的最后一条 ``time <= t`` 的下标。"""
    if not entries:
        return -1
    lo, hi, ans = 0, len(entries) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if entries[mid]["time"] <= t:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def group_by_line(entries: List[Dict]) -> List[List[Dict]]:
    """把同一 lineIdx 的 entries 聚成二维列表，便于前端渲染。"""
    if not entries:
        return []
    groups: List[List[Dict]] = []
    current_idx: Optional[int] = None
    current: List[Dict] = []
    for e in entries:
        if e["lineIdx"] != current_idx:
            if current:
                groups.append(current)
            current = [e]
            current_idx = e["lineIdx"]
        else:
            current.append(e)
    if current:
        groups.append(current)
    return groups
