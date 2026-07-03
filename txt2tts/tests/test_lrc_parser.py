"""Unit tests for ``app.services.lrc_parser`` (LRC parser + binary search).

前端 ``app/static/app.js`` 里的 ``parseLrc`` / ``findCurrentLrcIdx`` 应当与
本文件的实现行为一致；修改本文件时同步修改 JS。
"""
from __future__ import annotations

import pytest

from app.services.lrc_parser import (
    find_current_lrc_idx,
    group_by_line,
    parse_lrc,
)


# ---- parse_lrc -------------------------------------------------------------


def test_parse_lrc_empty_returns_empty_list():
    assert parse_lrc("") == []
    assert parse_lrc(None) == []  # type: ignore[arg-type]


def test_parse_lrc_basic_two_lines():
    text = "[00:01.00]第一句\n[00:03.50]第二句"
    entries = parse_lrc(text)
    assert len(entries) == 2
    assert entries[0] == {"time": 1.0, "text": "第一句", "lineIdx": 0}
    assert entries[1] == {"time": 3.5, "text": "第二句", "lineIdx": 1}


def test_parse_lrc_skips_meta_lines():
    text = (
        "[ti:歌名]\n"
        "[ar:作者]\n"
        "[al:专辑]\n"
        "[00:01.00]第一句\n"
    )
    entries = parse_lrc(text)
    assert len(entries) == 1
    assert entries[0]["text"] == "第一句"
    assert entries[0]["lineIdx"] == 0


def test_parse_lrc_skips_lines_without_timestamp():
    text = (
        "无时间戳的普通行\n"
        "[00:01.00]第一句\n"
        "又是无时间戳\n"
        "[00:03.00]第二句\n"
    )
    entries = parse_lrc(text)
    assert len(entries) == 2
    assert [e["text"] for e in entries] == ["第一句", "第二句"]


def test_parse_lrc_multiple_timestamps_one_line():
    """[00:01.00][00:05.00]重复歌词 → 展开为两条独立 entries。"""
    text = "[00:01.00][00:05.00]重复的副歌"
    entries = parse_lrc(text)
    assert len(entries) == 2
    assert entries[0]["time"] == 1.0
    assert entries[1]["time"] == 5.0
    assert entries[0]["text"] == "重复的副歌"
    assert entries[1]["text"] == "重复的副歌"
    # 不同时间 → 不同 lineIdx
    assert entries[0]["lineIdx"] == 0
    assert entries[1]["lineIdx"] == 1


def test_parse_lrc_consecutive_lines_same_time_share_line_idx():
    """同一时间戳的连续两行 → 共享同一 lineIdx（同步高亮）。"""
    text = "[00:01.00]上半句\n[00:01.00]下半句"
    entries = parse_lrc(text)
    assert len(entries) == 2
    assert entries[0]["lineIdx"] == entries[1]["lineIdx"] == 0
    assert entries[0]["time"] == entries[1]["time"] == 1.0


def test_parse_lrc_enhanced_milliseconds():
    """[mm:ss.xxx] 增强毫秒格式（3 位）→ 正确转秒。"""
    text = "[00:00.500]half-second\n[00:01.250]one-and-quarter"
    entries = parse_lrc(text)
    assert entries[0]["time"] == pytest.approx(0.5)
    assert entries[1]["time"] == pytest.approx(1.25)


def test_parse_lrc_fraction_short_form():
    """[00:01.5] 短小数（1 位）也按比例正确。"""
    text = "[00:01.5]一句"
    entries = parse_lrc(text)
    assert entries[0]["time"] == pytest.approx(1.5)


def test_parse_lrc_minutes_over_60_supported():
    """罕见但合法的 mm > 60 也按线性处理（不在断言范围但保证不抛错）。"""
    text = "[99:59.00]long"
    entries = parse_lrc(text)
    assert entries[0]["time"] == pytest.approx(99 * 60 + 59)


def test_parse_lrc_offset_meta_skipped():
    """[offset:500] 元信息行被跳过。"""
    text = "[offset:500]\n[00:00.00]第一句"
    entries = parse_lrc(text)
    assert len(entries) == 1
    assert entries[0]["text"] == "第一句"


def test_parse_lrc_empty_lyric_lines_kept():
    """[00:01.00] 后没有任何文字（乐器间奏）→ 保留作为空行，前端会显示空格。"""
    text = "[00:01.00]\n[00:03.00]第二句"
    entries = parse_lrc(text)
    assert len(entries) == 2
    assert entries[0]["text"] == ""
    assert entries[1]["text"] == "第二句"


def test_parse_lrc_handles_crlf():
    text = "[00:01.00]一行\r\n[00:02.00]二行"
    entries = parse_lrc(text)
    assert [e["text"] for e in entries] == ["一行", "二行"]


# ---- find_current_lrc_idx -----------------------------------------------


def test_find_current_before_first():
    entries = parse_lrc("[00:01.00]a\n[00:02.00]b")
    assert find_current_lrc_idx(entries, 0.5) == -1


def test_find_current_exact_match():
    entries = parse_lrc("[00:01.00]a\n[00:02.00]b\n[00:03.00]c")
    assert find_current_lrc_idx(entries, 1.0) == 0
    assert find_current_lrc_idx(entries, 2.0) == 1
    assert find_current_lrc_idx(entries, 3.0) == 2


def test_find_current_between():
    entries = parse_lrc("[00:01.00]a\n[00:02.00]b\n[00:03.00]c")
    # 1.5 在 a 和 b 之间 → 取 a (idx 0)
    assert find_current_lrc_idx(entries, 1.5) == 0
    # 2.5 在 b 和 c 之间 → 取 b (idx 1)
    assert find_current_lrc_idx(entries, 2.5) == 1


def test_find_current_after_last():
    entries = parse_lrc("[00:01.00]a\n[00:02.00]b")
    assert find_current_lrc_idx(entries, 99.0) == 1


def test_find_current_empty():
    assert find_current_lrc_idx([], 1.0) == -1


def test_find_current_single_entry():
    entries = parse_lrc("[00:01.00]only")
    assert find_current_lrc_idx(entries, 0.5) == -1
    assert find_current_lrc_idx(entries, 1.0) == 0
    assert find_current_lrc_idx(entries, 5.0) == 0


# ---- group_by_line --------------------------------------------------------


def test_group_by_line_basic():
    entries = parse_lrc("[00:01.00]a\n[00:02.00]b\n[00:03.00]c")
    groups = group_by_line(entries)
    assert len(groups) == 3
    assert all(len(g) == 1 for g in groups)
    assert [g[0]["text"] for g in groups] == ["a", "b", "c"]


def test_group_by_line_same_time_merged():
    """同一时间的连续多行合并为一组（前端用 <br> 拼接）。"""
    text = "[00:01.00]upper\n[00:01.00]lower\n[00:02.00]next"
    entries = parse_lrc(text)
    groups = group_by_line(entries)
    assert len(groups) == 2
    assert len(groups[0]) == 2
    assert [g[0]["text"] for g in groups] == ["upper", "next"]
    assert groups[0][0]["time"] == 1.0
    assert groups[0][1]["time"] == 1.0


def test_group_by_line_empty():
    assert group_by_line([]) == []


# ---- 端到端：从实际 LRC 文件中验证 ---------------------------------------


def test_parse_lrc_realistic_txt2tts_generated():
    """模拟 LyricsService.render_lrc 的真实输出（带元信息 + 大量行）。"""
    lines = []
    for i in range(20):
        lines.append(f"[{i // 60:02d}:{i % 60:02d}.00]第{i + 1}句歌词")
    text = "[ti:demo]\n[ar:txt2tts]\n[al:txt2tts]\n\n" + "\n".join(lines)
    entries = parse_lrc(text)
    assert len(entries) == 20
    assert entries[0] == {"time": 0.0, "text": "第1句歌词", "lineIdx": 0}
    assert entries[19] == {"time": 19.0, "text": "第20句歌词", "lineIdx": 19}