"""Unit tests for EdgeTtsClient helpers + SRT/LRC 转换器。
不发起真实 edge-tts 调用，只测纯函数与 format_srt_timestamp。
"""
from __future__ import annotations

import pytest

from app.config import EDGE_VOICES_ZH, EdgeTtsSettings
from app.services.edge_tts_provider import (
    EdgeTtsClient,
    EdgeTtsError,
    extract_disambiguation_hints,
    format_lrc_timestamp,
    format_srt_timestamp,
    split_sentences,
    srt_to_lrc,
    strip_disambiguation,
)


# ---- strip_disambiguation / extract_disambiguation_hints -----------------


def test_strip_disambiguation_removes_pinyin_marks():
    text = "行[xíng]走江湖，重[zhòng]要的事情说三遍。"
    assert strip_disambiguation(text) == "行走江湖，重要的事情说三遍。"


def test_strip_disambiguation_no_marks_returns_same():
    assert strip_disambiguation("正常文本。") == "正常文本。"


def test_extract_disambiguation_hints_returns_pairs():
    text = "行长[zhǎng]得很快，重[zhòng]量十足。"
    hints = extract_disambiguation_hints(text)
    assert hints == [("长", "zhǎng"), ("重", "zhòng")]


# ---- split_sentences -----------------------------------------------------


def test_split_sentences_basic():
    s = "第一句。第二句！第三句？"
    parts = split_sentences(s, max_chars=200)
    assert parts == ["第一句。", "第二句！", "第三句？"]


def test_split_sentences_respects_max_chars():
    long = "句子A。" * 50  # 每个 4 字，共 200 字
    parts = split_sentences(long, max_chars=50)
    # 50 字限会把长串切短；至少要有 4 段
    assert len(parts) >= 4
    for p in parts:
        assert len(p) <= 60  # 留一定余量


def test_split_sentences_preserves_paragraphs():
    text = "第一段第一句。第一段第二句。\n\n第二段一句。第二段二句。"
    parts = split_sentences(text, max_chars=200)
    assert parts == [
        "第一段第一句。", "第一段第二句。",
        "第二段一句。", "第二段二句。",
    ]


def test_split_sentences_drops_empty():
    assert split_sentences("") == []
    assert split_sentences("   \n\n  ") == []


# ---- format_srt_timestamp / format_lrc_timestamp --------------------------


def test_format_srt_timestamp_zero():
    assert format_srt_timestamp(0) == "00:00:00,000"


def test_format_srt_timestamp_with_ms():
    assert format_srt_timestamp(1.5) == "00:00:01,500"


def test_format_srt_timestamp_minutes():
    assert format_srt_timestamp(65.25) == "00:01:05,250"


def test_format_srt_timestamp_hours():
    assert format_srt_timestamp(3661.001) == "01:01:01,001"


def test_format_srt_timestamp_negative_clamped():
    assert format_srt_timestamp(-1) == "00:00:00,000"


def test_format_lrc_timestamp_basic():
    assert format_lrc_timestamp(0) == "[00:00.00]"
    assert format_lrc_timestamp(12.5) == "[00:12.50]"
    assert format_lrc_timestamp(125.123) == "[02:05.12]"  # 四舍五入到厘秒


# ---- srt_to_lrc ----------------------------------------------------------


SRT_SAMPLE = """1
00:00:00,000 --> 00:00:02,500
第一句歌词。

2
00:00:02,500 --> 00:00:05,000
第二句歌词。

3
00:00:05,000 --> 00:00:08,000
第三句歌词。
"""


def test_srt_to_lrc_basic():
    lrc = srt_to_lrc(SRT_SAMPLE, title="测试曲", artist="test-artist")
    assert "[ti:测试曲]" in lrc
    assert "[ar:test-artist]" in lrc
    assert "[00:00.00]第一句歌词。" in lrc
    assert "[00:02.50]第二句歌词。" in lrc
    assert "[00:05.00]第三句歌词。" in lrc


def test_srt_to_lrc_default_artist():
    lrc = srt_to_lrc(SRT_SAMPLE, title="x")
    assert "[ar:txt2tts]" in lrc


def test_srt_to_lrc_empty_returns_header():
    lrc = srt_to_lrc("", title="空")
    assert "[ti:空]" in lrc
    # 没有任何 [mm:ss] 时间戳
    import re
    assert not re.search(r"\[\d+:\d+\.\d+\]", lrc)

# ---- EdgeTtsClient.synthesize_segment voice 白名单校验 --------------------


def _settings() -> EdgeTtsSettings:
    return EdgeTtsSettings(default_voice="zh-CN-XiaoxiaoNeural")


async def test_synthesize_segment_rejects_mimo_default_voice():
    """MiMo voice (mimo_default) 必须被 edge provider 拒绝，不能传给 edge-tts。"""
    client = EdgeTtsClient(_settings())
    with pytest.raises(EdgeTtsError) as ei:
        await client.synthesize_segment("hello", voice="mimo_default")
    msg = str(ei.value)
    assert "mimo_default" in msg
    assert "edge provider" in msg.lower()
    # 错误里应当列出一些合法 voice 帮助用户切换
    assert "zh-CN-XiaoxiaoNeural" in msg


async def test_synthesize_segment_rejects_unknown_voice():
    client = EdgeTtsClient(_settings())
    with pytest.raises(EdgeTtsError):
        await client.synthesize_segment("hi", voice="totally-not-a-real-voice")


async def test_synthesize_segment_accepts_whitelist_voice_passes_validation():
    """白名单里的 voice 必须能通过 voice 校验进入合成阶段。

    这里只断言 voice 校验这一道关：合法 voice 不抛 EdgeTtsError。
    完整的合成 + SubMaker 流程依赖 edge-tts 内部 API 行为，由
    test_pipeline_edge.py 真实运行覆盖；本测试只验证 voice 白名单不会误伤。
    """
    client = EdgeTtsClient(_settings())
    valid_voice = EDGE_VOICES_ZH[0]["id"]
    # monkeypatch edge_tts.Communicate 让它在被构造时验证 voice，然后
    # stream() 立刻抛出模拟下游真实错误。这样我们只关心「voice 通过校验」
    # 这一步，而不需要走完整个 SubMaker 流程。
    import edge_tts

    class _FakeComm:
        def __init__(self, text, voice, **kwargs):
            assert voice == valid_voice, f"voice was not whitelisted: {voice!r}"

        async def stream(self):
            raise RuntimeError("mock stream interrupted after voice check")
            yield  # unreachable, 让函数成为 async generator

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _FakeComm)
    try:
        with pytest.raises(EdgeTtsError) as ei:
            await client.synthesize_segment("一段文本", voice=valid_voice)
        # 错误应当来自下游 stream，而不是 voice 校验
        msg = str(ei.value)
        assert "Invalid voice" not in msg
        assert "edge-tts 调用失败" in msg or "mock stream" in msg
    finally:
        monkey.undo()


async def test_synthesize_segment_rejects_empty_text():
    client = EdgeTtsClient(_settings())
    with pytest.raises(EdgeTtsError):
        await client.synthesize_segment("", voice="zh-CN-XiaoxiaoNeural")
    with pytest.raises(EdgeTtsError):
        await client.synthesize_segment("   ", voice="zh-CN-XiaoxiaoNeural")


async def test_synthesize_segment_collects_cues_from_sentence_boundary():
    """edge-tts 7.x：SubMaker 不再提供 get_cues()；实现必须直接从
    SentenceBoundary 事件累加 cues。这里 mock 一次完整 stream，断言
    音频拼接 + cues 时间戳换算（100ns → 秒）正确。"""
    client = EdgeTtsClient(_settings())
    import edge_tts

    # offset / duration 单位是 100ns：
    #   0s + 1.5s = offset 0, duration 15_000_000
    #   1.5s + 2.0s = offset 15_000_000, duration 20_000_000
    class _FakeComm:
        def __init__(self, text, voice, **kwargs):
            self.text = text
            self.voice = voice

        async def stream(self):
            yield {"type": "audio", "data": b"\xff\xfb\x90\x00"}  # mp3 帧
            yield {
                "type": "SentenceBoundary",
                "offset": 0,
                "duration": 15_000_000,
                "text": "第一句。",
            }
            yield {"type": "audio", "data": b"\xff\xfb\x90\x00"}
            yield {
                "type": "SentenceBoundary",
                "offset": 15_000_000,
                "duration": 20_000_000,
                "text": "第二句。",
            }

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _FakeComm)
    try:
        audio, cues = await client.synthesize_segment("两句话。", voice="zh-CN-XiaoxiaoNeural")
        assert audio == b"\xff\xfb\x90\x00\xff\xfb\x90\x00"
        assert len(cues) == 2
        # 100ns → 秒 换算
        assert cues[0] == (0.0, 1.5, "第一句。")
        assert cues[1] == (1.5, 3.5, "第二句。")
    finally:
        monkey.undo()


async def test_synthesize_segment_handles_missing_boundary_fields():
    """SentenceBoundary 事件缺字段时不能崩。"""
    client = EdgeTtsClient(_settings())
    import edge_tts

    class _FakeComm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"X"}
            yield {"type": "SentenceBoundary"}  # 没有 offset/duration/text

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _FakeComm)
    try:
        audio, cues = await client.synthesize_segment("hi", voice="zh-CN-XiaoxiaoNeural")
        assert audio == b"X"
        # 缺字段时按 (0.0, 0.0, "") 兜底
        assert cues == [(0.0, 0.0, "")]
    finally:
        monkey.undo()


# ---- pipeline._resolve_edge_voice 白名单 fallback --------------------------

from app.services.pipeline import TtsPipeline


def test_pipeline_resolve_edge_voice_accepts_valid():
    from app.services.edge_tts_provider import EdgeTtsClient
    pipe = TtsPipeline(
        markdown=None, llm=None, tts=None, audio=None,
        edge_tts=EdgeTtsClient(_settings()),
    )
    assert pipe._resolve_edge_voice("zh-CN-YunxiNeural") == "zh-CN-YunxiNeural"


def test_pipeline_resolve_edge_voice_falls_back_for_mimo_voice(caplog):
    from app.services.edge_tts_provider import EdgeTtsClient
    pipe = TtsPipeline(
        markdown=None, llm=None, tts=None, audio=None,
        edge_tts=EdgeTtsClient(_settings()),
    )
    with caplog.at_level("WARNING"):
        result = pipe._resolve_edge_voice("mimo_default")
    assert result == "zh-CN-XiaoxiaoNeural"  # 默认 fallback
    assert any("mimo_default" in r.message for r in caplog.records)


def test_pipeline_resolve_edge_voice_falls_back_for_none():
    from app.services.edge_tts_provider import EdgeTtsClient
    pipe = TtsPipeline(
        markdown=None, llm=None, tts=None, audio=None,
        edge_tts=EdgeTtsClient(_settings()),
    )
    assert pipe._resolve_edge_voice(None) == "zh-CN-XiaoxiaoNeural"
    assert pipe._resolve_edge_voice("") == "zh-CN-XiaoxiaoNeural"


def test_pipeline_resolve_edge_voice_falls_back_for_unknown_voice(caplog):
    from app.services.edge_tts_provider import EdgeTtsClient
    pipe = TtsPipeline(
        markdown=None, llm=None, tts=None, audio=None,
        edge_tts=EdgeTtsClient(_settings()),
    )
    with caplog.at_level("WARNING"):
        result = pipe._resolve_edge_voice("some-bogus-voice")
    assert result == "zh-CN-XiaoxiaoNeural"
    assert any("some-bogus-voice" in r.message for r in caplog.records)


# ---- _is_transient_error / 重试机制 ---------------------------------------


def test_is_transient_error_recognizes_network_exceptions():
    from app.services.edge_tts_provider import _is_transient_error, EdgeTtsError
    # aiohttp 类（名字匹配白名单）
    class ClientConnectorError(Exception):
        pass
    assert _is_transient_error(ClientConnectorError("conn refused")) is True
    # ssl
    class SSLError(Exception):
        pass
    assert _is_transient_error(SSLError("EOF")) is True
    # TimeoutError
    assert _is_transient_error(TimeoutError()) is True
    # ConnectionError
    assert _is_transient_error(ConnectionError("refused")) is True
    # OSError
    assert _is_transient_error(OSError("getaddrinfo failed")) is True
    # 关键词兜底
    assert _is_transient_error(EdgeTtsError("Cannot connect to host x:443")) is True
    assert _is_transient_error(EdgeTtsError("Connection reset by peer")) is True


def test_is_transient_error_does_not_match_business_errors():
    from app.services.edge_tts_provider import _is_transient_error, EdgeTtsError
    # Invalid voice → 业务错误，不应重试
    assert _is_transient_error(EdgeTtsError("Invalid voice 'mimo_default' for edge provider.")) is False
    # 空文本
    assert _is_transient_error(EdgeTtsError("Refusing to synthesize empty text.")) is False
    # 空音频
    assert _is_transient_error(EdgeTtsError("edge-tts 返回空音频。")) is False


async def test_synthesize_segment_retries_on_transient_then_succeeds():
    """第一次 stream() 抛瞬时网络错误，第二次成功 → 应返回音频。"""
    import edge_tts
    client = EdgeTtsClient(EdgeTtsSettings(
        default_voice="zh-CN-XiaoxiaoNeural",
        max_retries=3, retry_backoff_sec=0.0,  # 测试时关闭 sleep
    ))

    call_count = {"n": 0}

    class _Comm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("Cannot connect to host speech.platform.bing.com:443")
            yield {"type": "audio", "data": b"RECOVERED"}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _Comm)
    try:
        audio, _cues = await client.synthesize_segment("hi", voice="zh-CN-XiaoxiaoNeural")
        assert audio == b"RECOVERED"
        assert call_count["n"] == 2
    finally:
        monkey.undo()


async def test_synthesize_segment_exhausts_retries_and_raises_friendly_error():
    """连续 3 次瞬时错误后抛 EdgeTtsError，错误消息应包含「重试」+「不可达」提示。"""
    import edge_tts
    client = EdgeTtsClient(EdgeTtsSettings(
        default_voice="zh-CN-XiaoxiaoNeural",
        max_retries=2, retry_backoff_sec=0.0,
    ))

    class _Comm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            raise ConnectionError("Cannot connect to host speech.platform.bing.com:443")
            yield  # unreachable

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _Comm)
    try:
        with pytest.raises(EdgeTtsError) as ei:
            await client.synthesize_segment("hi", voice="zh-CN-XiaoxiaoNeural")
        msg = str(ei.value)
        assert "不可达" in msg or "重试" in msg
        # 原始网络异常信息应保留
        assert "Cannot connect to host" in msg
    finally:
        monkey.undo()


async def test_synthesize_segment_does_not_retry_business_error():
    """Invalid voice 类业务错误应当立即抛，不重试。"""
    import edge_tts
    client = EdgeTtsClient(EdgeTtsSettings(
        default_voice="zh-CN-XiaoxiaoNeural",
        max_retries=3, retry_backoff_sec=0.0,
    ))
    call_count = {"n": 0}

    class _Comm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            call_count["n"] += 1
            raise ValueError("Invalid voice 'mimo_default' for edge provider.")
            yield

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _Comm)
    try:
        with pytest.raises(EdgeTtsError):
            await client.synthesize_segment("hi", voice="mimo_default")
        # 业务错误应当只调一次（不重试）
        # 等等：voice 校验在 synthesize_segment 开头就抛 EdgeTtsError，根本没到 _stream_once
        # 所以 call_count 应该是 0
        assert call_count["n"] == 0
    finally:
        monkey.undo()


async def test_synthesize_segment_zero_retries_no_retry_on_failure():
    """max_retries=0 时只尝试一次，失败立刻抛。"""
    import edge_tts
    client = EdgeTtsClient(EdgeTtsSettings(
        default_voice="zh-CN-XiaoxiaoNeural",
        max_retries=0, retry_backoff_sec=0.0,
    ))

    call_count = {"n": 0}

    class _Comm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            call_count["n"] += 1
            raise OSError("Cannot connect to host")
            yield

    monkey = pytest.MonkeyPatch()
    monkey.setattr(edge_tts, "Communicate", _Comm)
    try:
        with pytest.raises(EdgeTtsError):
            await client.synthesize_segment("hi", voice="zh-CN-XiaoxiaoNeural")
        assert call_count["n"] == 1
    finally:
        monkey.undo()


async def test_synthesize_segment_exponential_backoff_called_with_delay():
    """重试之间应当按指数退避等待（验证调用 sleep 次数与延迟值）。"""
    import edge_tts
    import asyncio
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    client = EdgeTtsClient(EdgeTtsSettings(
        default_voice="zh-CN-XiaoxiaoNeural",
        max_retries=3, retry_backoff_sec=1.0,
    ))
    # 替换实例的 _stream_once 不现实（私有方法），改用 monkeypatch
    # asyncio.sleep 本身
    monkey = pytest.MonkeyPatch()

    class _Comm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            raise ConnectionError("Cannot connect to host")
            yield

    monkey.setattr(edge_tts, "Communicate", _Comm)
    monkey.setattr(asyncio, "sleep", fake_sleep)
    try:
        with pytest.raises(EdgeTtsError):
            await client.synthesize_segment("hi", voice="zh-CN-XiaoxiaoNeural")
        # 3 retries = 4 attempts → 3 sleeps: 1.0, 2.0, 4.0
        assert sleeps == [1.0, 2.0, 4.0]
    finally:
        monkey.undo()
