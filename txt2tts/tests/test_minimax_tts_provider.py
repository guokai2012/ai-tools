"""Tests for MinimaxTtsClient.

Covers:
    * T2A HTTP request body shape (subtitle_enable / subtitle_type / voice / audio setting)
    * Response parsing (hex audio decode, extra_info.audio_length, base_resp.status_code)
    * subtitle_file 二次 GET + parse
    * SubtitleFetchError on subtitle failures (404 / 非 JSON / 字段名未知)
    * 5xx retry / 4xx 不重试
    * voice 白名单校验 + fallback
    * api_key 回落 LLM__API_KEY
    * 空文本 / 长文本边界
"""
from __future__ import annotations

import json
import pytest
import respx
from httpx import Response

from app.config import MinimaxTtsSettings
from app.services.minimax_tts_provider import (
    MinimaxTtsClient,
    MinimaxTtsError,
    ProviderResult,
    SubtitleFetchError,
    _pick_key,
)


# ---- fixtures --------------------------------------------------------------


def _settings(**overrides) -> MinimaxTtsSettings:
    defaults = dict(
        api_key="test-minimax-key",
        base_url="https://api.minimaxi.com",
        t2a_path="/v1/t2a_v2",
        model="speech-2.8-hd",
        voice_id="male-qn-qingse",
        audio_format="mp3",
        sample_rate=32000,
        bitrate=128000,
        audio_channel=1,
        speed=1.0,
        vol=1.0,
        pitch=0,
        subtitle_type="sentence",
        subtitle_fetch_timeout_sec=5.0,
        max_input_chars_per_request=10000,
        request_timeout_sec=30.0,
        max_retries=2,
    )
    defaults.update(overrides)
    return MinimaxTtsSettings(**defaults)


def _audio_hex(b: bytes) -> str:
    return b.hex()


FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 100
SUBTITLE_URL = "https://minimaxi-oss.example.com/subtitles/abc.json?token=xyz"


def _t2a_ok(audio_bytes: bytes = FAKE_MP3, audio_length_ms: int = 1102) -> dict:
    """构造 T2A HTTP 成功响应（含 subtitle_file）。"""
    return {
        "data": {
            "audio": _audio_hex(audio_bytes),
            "subtitle_file": SUBTITLE_URL,
            "status": 2,
        },
        "extra_info": {
            "audio_length": audio_length_ms,
            "audio_size": len(audio_bytes),
            "audio_sample_rate": 32000,
            "bitrate": 128000,
            "word_count": 30,
            "usage_characters": 30,
            "audio_format": "mp3",
            "audio_channel": 1,
        },
        "trace_id": "trace-abc",
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def _subtitle_json_sentence() -> list:
    """句级字幕 JSON（text / start_time / end_time 字段名，毫秒）。"""
    return [
        {"text": "你好世界", "start_time": 0, "end_time": 1500},
        {"text": "这是第二句", "start_time": 1500, "end_time": 3000},
        {"text": "第三句结尾", "start_time": 3000, "end_time": 4500},
    ]


# ---- 1. 请求体形态 --------------------------------------------------------


class TestRequestBody:
    def test_request_body_includes_subtitle_enable(self):
        client = MinimaxTtsClient(_settings())
        body = client._build_request_body(text="你好", voice_id="male-qn-qingse")
        assert body["subtitle_enable"] is True
        assert body["subtitle_type"] == "sentence"
        assert body["model"] == "speech-2.8-hd"
        assert body["stream"] is False
        assert body["text"] == "你好"

    def test_request_body_voice_setting_shape(self):
        client = MinimaxTtsClient(_settings(speed=1.5, vol=2.0, pitch=-3))
        body = client._build_request_body(text="x", voice_id="female-shaonv")
        vs = body["voice_setting"]
        # v3 起 voice_setting 额外带 emotion + text_normalization 字段
        assert vs["voice_id"] == "female-shaonv"
        assert vs["speed"] == 1.5
        assert vs["vol"] == 2.0
        assert vs["pitch"] == -3
        assert vs["emotion"] == "calm"
        assert vs["text_normalization"] is True

    def test_request_body_audio_setting_shape(self):
        client = MinimaxTtsClient(_settings(
            audio_format="mp3", sample_rate=44100, bitrate=256000, audio_channel=2,
        ))
        body = client._build_request_body(text="x", voice_id="male-qn-qingse")
        assert body["audio_setting"] == {
            "sample_rate": 44100,
            "bitrate": 256000,
            "format": "mp3",
            "channel": 2,
        }


# ---- 2. 响应解析 ----------------------------------------------------------


class TestResponseParsing:
    @pytest.mark.asyncio
    @respx.mock
    async def test_parses_hex_audio_and_audio_length(self):
        client = MinimaxTtsClient(_settings())
        # 准备：subtitle_file 子调用
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence())
        )
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok(audio_length_ms=1102))
        )
        result = await client.synthesize_segment("你好世界这是测试文本")
        assert isinstance(result, ProviderResult)
        assert result.audio_bytes == FAKE_MP3
        assert abs(result.duration_sec - 1.102) < 0.01
        assert result.subtitle_fetch_error is None
        assert len(result.sentence_cues) == 3
        # 第一句：0~1.5s
        assert result.sentence_cues[0] == (0.0, 1.5, "你好世界")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_response_non_json_raises(self):
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, text="<html>error</html>")
        )
        with pytest.raises(MinimaxTtsError, match="non-JSON"):
            await client.synthesize_segment("hi")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_base_resp_error_raises(self):
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json={
                "base_resp": {"status_code": 1004, "status_msg": "鉴权失败"},
                "data": {}, "extra_info": {}, "trace_id": "t",
            })
        )
        with pytest.raises(MinimaxTtsError, match="1004"):
            await client.synthesize_segment("hi")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_audio_raises(self):
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json={
                "base_resp": {"status_code": 0},
                "data": {"audio": ""},
                "extra_info": {"audio_length": 1000},
                "trace_id": "t",
            })
        )
        with pytest.raises(MinimaxTtsError, match="no audio data"):
            await client.synthesize_segment("hi")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_hex_raises(self):
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json={
                "base_resp": {"status_code": 0},
                "data": {"audio": "NOT_HEX_VALUE"},  # 偶数位但非 hex
                "extra_info": {"audio_length": 1000},
                "trace_id": "t",
            })
        )
        with pytest.raises(MinimaxTtsError, match="hex audio"):
            await client.synthesize_segment("hi")
        await client.aclose()


# ---- 3. subtitle_file 二次 GET ---------------------------------------------


class TestSubtitleFetch:
    @pytest.mark.asyncio
    @respx.mock
    async def test_subtitle_404_marks_error_audio_still_available(self):
        """字幕 URL 404 → ProviderResult.subtitle_fetch_error 非空；音频仍可用。"""
        client = MinimaxTtsClient(_settings())
        respx.get(SUBTITLE_URL).mock(return_value=Response(404, text="Not Found"))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok())
        )
        result = await client.synthesize_segment("hi")
        assert result.audio_bytes == FAKE_MP3
        assert result.subtitle_fetch_error is not None
        assert "404" in result.subtitle_fetch_error
        assert result.sentence_cues == []
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_subtitle_non_json_marks_error(self):
        client = MinimaxTtsClient(_settings())
        respx.get(SUBTITLE_URL).mock(return_value=Response(200, text="<not json>"))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok())
        )
        result = await client.synthesize_segment("hi")
        assert result.audio_bytes == FAKE_MP3
        assert "非 JSON" in (result.subtitle_fetch_error or "")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_subtitle_unknown_field_names_marks_error(self):
        """字段名不匹配候选 → ProviderResult.subtitle_fetch_error 含诊断信息。"""
        client = MinimaxTtsClient(_settings())
        weird_payload = [{"foo": "bar", "ts": 0, "te": 100}]
        respx.get(SUBTITLE_URL).mock(return_value=Response(200, json=weird_payload))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok())
        )
        result = await client.synthesize_segment("hi")
        assert result.audio_bytes == FAKE_MP3
        assert "字段名无法识别" in (result.subtitle_fetch_error or "")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_subtitle_alternate_field_names(self):
        """sentence_text / begin_time / finish_time 等候选字段也应被识别。"""
        client = MinimaxTtsClient(_settings())
        alt_payload = [
            {"sentence_text": "句子A", "begin_time": 0, "finish_time": 2000},
            {"sentence_text": "句子B", "begin_time": 2000, "finish_time": 4000},
        ]
        respx.get(SUBTITLE_URL).mock(return_value=Response(200, json=alt_payload))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok())
        )
        result = await client.synthesize_segment("hi")
        assert result.subtitle_fetch_error is None
        assert len(result.sentence_cues) == 2
        assert result.sentence_cues[0] == (0.0, 2.0, "句子A")
        assert result.sentence_cues[1] == (2.0, 4.0, "句子B")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_subtitle_file_in_response(self):
        """响应不带 subtitle_file → ProviderResult.subtitle_fetch_error 非空；音频仍可用。"""
        client = MinimaxTtsClient(_settings())
        payload_no_sub = _t2a_ok()
        del payload_no_sub["data"]["subtitle_file"]
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=payload_no_sub)
        )
        result = await client.synthesize_segment("hi")
        assert result.audio_bytes == FAKE_MP3
        assert "未返回 subtitle_file" in (result.subtitle_fetch_error or "")
        await client.aclose()


# ---- 4. retry 行为 --------------------------------------------------------


class TestRetryBehavior:
    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_retried(self):
        """5xx 自动重试 max_retries+1 次。"""
        client = MinimaxTtsClient(_settings(max_retries=2))
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence())
        )
        # 前两次 500，第三次 200
        route = respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            side_effect=[
                Response(500, text="server error"),
                Response(502, text="bad gateway"),
                Response(200, json=_t2a_ok()),
            ]
        )
        result = await client.synthesize_segment("hi")
        assert result.audio_bytes == FAKE_MP3
        assert route.call_count == 3
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_exhausted_raises(self):
        client = MinimaxTtsClient(_settings(max_retries=1))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(500, text="always 500")
        )
        with pytest.raises(MinimaxTtsError, match="after retries"):
            await client.synthesize_segment("hi")
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_4xx_no_retry(self):
        """4xx 不重试，直接抛错。"""
        client = MinimaxTtsClient(_settings(max_retries=5))
        route = respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(400, text="bad voice_id")
        )
        with pytest.raises(MinimaxTtsError, match="400"):
            await client.synthesize_segment("hi")
        assert route.call_count == 1
        await client.aclose()


# ---- 5. voice 白名单 -------------------------------------------------------


class TestVoiceResolution:
    def test_valid_voice_passes_through(self):
        client = MinimaxTtsClient(_settings(voice_id="male-qn-qingse"))
        assert client._resolve_voice("female-shaonv") == "female-shaonv"

    def test_invalid_voice_falls_back(self):
        client = MinimaxTtsClient(_settings(voice_id="male-qn-qingse"))
        assert client._resolve_voice("mimo_default") == "male-qn-qingse"  # 来自白名单外

    def test_none_voice_uses_default(self):
        client = MinimaxTtsClient(_settings(voice_id="female-yujie"))
        assert client._resolve_voice(None) == "female-yujie"

    def test_empty_voice_uses_default(self):
        client = MinimaxTtsClient(_settings(voice_id="English_Graceful_Lady"))
        assert client._resolve_voice("") == "English_Graceful_Lady"


# ---- 6. api_key 回落 ------------------------------------------------------


class TestApiKeyFallback:
    def test_settings_api_key_used_when_set(self):
        client = MinimaxTtsClient(_settings(api_key="explicit-key"))
        assert client._effective_api_key == "explicit-key"

    def test_fallback_to_llm_key(self):
        client = MinimaxTtsClient(_settings(api_key=""), api_key_fallback="llm-key")
        assert client._effective_api_key == "llm-key"

    def test_no_api_key_either(self):
        client = MinimaxTtsClient(_settings(api_key=""), api_key_fallback="")
        assert client._effective_api_key == ""

    @pytest.mark.asyncio
    async def test_empty_api_key_raises_on_synthesize(self):
        client = MinimaxTtsClient(_settings(api_key=""), api_key_fallback="")
        with pytest.raises(MinimaxTtsError, match="API key is not configured"):
            await client.synthesize_segment("hi")
        await client.aclose()


# ---- 7. 边界 --------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_text_raises(self):
        client = MinimaxTtsClient(_settings())
        with pytest.raises(MinimaxTtsError, match="empty text"):
            await client.synthesize_segment("")
        with pytest.raises(MinimaxTtsError, match="empty text"):
            await client.synthesize_segment("   ")
        await client.aclose()

    def test_url_construction(self):
        s = _settings(base_url="https://api.minimaxi.com/", t2a_path="/v1/t2a_v2")
        assert s.t2a_url == "https://api.minimaxi.com/v1/t2a_v2"
        s2 = _settings(base_url="https://api.minimaxi.com", t2a_path="/v1/t2a_v2")
        assert s2.t2a_url == "https://api.minimaxi.com/v1/t2a_v2"


# ---- 8. _pick_key helper --------------------------------------------------


class TestPickKey:
    def test_finds_first_present(self):
        assert _pick_key({"a": 1, "b": 2, "c": 3}, ("a", "b")) == "a"
        assert _pick_key({"b": 2}, ("a", "b")) == "b"

    def test_returns_none_when_missing(self):
        assert _pick_key({"x": 1}, ("a", "b")) is None
        assert _pick_key({}, ("a",)) is None


# ---- 9. _parse_subtitle_payload（白盒）----------------------------------


class TestParseSubtitlePayload:
    def test_parses_valid_list(self):
        cues = MinimaxTtsClient._parse_subtitle_payload([
            {"text": "a", "start_time": 0, "end_time": 1000},
            {"text": "b", "start_time": 1000, "end_time": 2000},
        ])
        assert cues == [(0.0, 1.0, "a"), (1.0, 2.0, "b")]

    def test_unwraps_subtitles_dict(self):
        cues = MinimaxTtsClient._parse_subtitle_payload({
            "subtitles": [{"text": "x", "start_time": 0, "end_time": 500}],
        })
        assert cues == [(0.0, 0.5, "x")]

    def test_non_list_non_dict_raises(self):
        with pytest.raises(SubtitleFetchError, match="不是 list"):
            MinimaxTtsClient._parse_subtitle_payload("not a list")

    def test_empty_list_raises(self):
        with pytest.raises(SubtitleFetchError, match="空 list"):
            MinimaxTtsClient._parse_subtitle_payload([])

    def test_skip_entries_with_missing_fields(self):
        """部分 entry 缺字段 → 跳过；其余正常。"""
        cues = MinimaxTtsClient._parse_subtitle_payload([
            {"text": "ok", "start_time": 0, "end_time": 100},
            {"foo": "no fields"},  # 跳过
            {"text": "ok2", "start_time": 100, "end_time": 200},
        ])
        assert len(cues) == 2

    def test_all_invalid_raises_with_sample(self):
        """所有 entry 字段都不可识别 → 抛错并附带 sample。"""
        with pytest.raises(SubtitleFetchError, match="字段名无法识别"):
            MinimaxTtsClient._parse_subtitle_payload([
                {"foo": "bar"},
                {"baz": "qux"},
            ])

    def test_skip_entries_with_empty_text(self):
        """text 字段为空字符串 → 跳过该 entry（无意义）。"""
        cues = MinimaxTtsClient._parse_subtitle_payload([
            {"text": "", "start_time": 0, "end_time": 100},
            {"text": "real", "start_time": 100, "end_time": 200},
        ])
        assert cues == [(0.1, 0.2, "real")]

    def test_millisecond_to_second_conversion(self):
        """毫秒数必须 / 1000 转秒（验证 5000ms = 5s）。"""
        cues = MinimaxTtsClient._parse_subtitle_payload([
            {"text": "long", "start_time": 0, "end_time": 5000},
        ])
        assert cues[0] == (0.0, 5.0, "long")


# ---- 10. list_voices ------------------------------------------------------


class TestListVoices:
    @pytest.mark.asyncio
    async def test_returns_minimax_whitelist(self):
        client = MinimaxTtsClient(_settings())
        voices, source = await client.list_voices()
        assert source == "static"
        ids = {v["id"] for v in voices}
        # 必须包含一些白名单 voice
        assert "male-qn-qingse" in ids
        assert "female-shaonv" in ids
        # 必须有 lang 字段
        for v in voices:
            assert "id" in v and "name" in v and "lang" in v
        await client.aclose()


# ---- v3 起：output_format=url + OSS 临时 URL 下载路径 ----------------------


AUDIO_URL = "https://oss.example.com/audio/abc.mp3?Expires=123&Signature=xyz"
SUBTITLE_URL = "https://oss.example.com/subtitles/abc.json?Expires=456&Signature=uvw"


def _t2a_ok_with_url_audio(
    audio_url: str = AUDIO_URL,
    subtitle_url: str = SUBTITLE_URL,
    audio_length_ms: int = 9252,
) -> dict:
    """response.data.audio 是 OSS URL + subtitle_file 是 OSS URL（v3 形态）。"""
    return {
        "data": {
            "audio": audio_url,
            "subtitle_file": subtitle_url,
            "status": 2,
        },
        "extra_info": {
            "audio_length": audio_length_ms,
            "audio_size": 149748,
            "audio_sample_rate": 32000,
            "bitrate": 128000,
            "word_count": 52,
            "usage_characters": 101,
            "audio_format": "mp3",
            "audio_channel": 1,
        },
        "trace_id": "trace-abc",
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


class TestUrlOutput:
    """v3 起 output_format=url：data.audio 是 OSS 临时 URL，客户端做下载。"""

    @pytest.mark.asyncio
    @respx.mock
    async def test_audio_url_downloaded_then_decoded(self):
        """data.audio 是 https URL → _download_with_retry 命中 → 返回 mp3 bytes。"""
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        # mock OSS URL GET：返回 mp3 bytes
        respx.get(AUDIO_URL).mock(
            return_value=Response(200, content=FAKE_MP3),
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        result = await client.synthesize_segment(
            "短文", voice="male-qn-qingse", title="demo",
        )
        assert result.audio_bytes == FAKE_MP3
        assert abs(result.duration_sec - 9.252) < 0.01
        # subtitle URL 下载 + 解析仍正常
        assert len(result.sentence_cues) == 3
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_audio_hex_path_still_works_when_not_url(self):
        """v3 起 output_format=hex（老接口）仍可工作，向后兼容。"""
        client = MinimaxTtsClient(_settings(output_format="hex"))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok(audio_length_ms=1102)),
        )
        # subtitle URL 仍按 v3 走
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        result = await client.synthesize_segment(
            "短文", voice="male-qn-qingse", title="demo",
        )
        assert result.audio_bytes == FAKE_MP3
        assert len(result.sentence_cues) == 3
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_request_body_contains_output_format_url_and_voice_settings(self):
        """请求体 output_format=url + voice_setting 必含 emotion / text_normalization。"""
        client = MinimaxTtsClient(_settings())
        body = client._build_request_body(text="hi", voice_id="male-qn-qingse")
        assert body["output_format"] == "url"
        vs = body["voice_setting"]
        assert "emotion" in vs
        assert "text_normalization" in vs
        await client.aclose()


class TestUrlDownloadRetry:
    """v4 起：OSS URL 下载走指数退避，仅 5xx/429/网络错触发，其他 raise。"""

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_5xx_retries_then_succeeds(self, monkeypatch):
        """audio URL 第 1-2 次 503，第 3 次 200 → 应拿到 mp3 bytes + 2 次 sleep。"""
        # fake clock 抽走 sleep，避免真实等待
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)

        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        # 第一次 GET → 503，第二次 → 503，第三次 → 200
        respx.get(AUDIO_URL).mock(
            side_effect=[
                Response(503, text="Server Error"),
                Response(503, text="Server Error"),
                Response(200, content=FAKE_MP3),
            ],
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )

        result = await client.synthesize_segment("x", voice="male-qn-qingse")
        assert result.audio_bytes == FAKE_MP3
        # 5xx 重试 sleep：第 1 次失败后 sleep 1s，第 2 次失败后 sleep 2s；第 3 次成功不 sleep
        assert sleeps == [1.0, 2.0]
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_5xx_exhausts_5_retries_raises(self, monkeypatch):
        """OSS URL 5 次都 503 → raise MinimaxTtsError 含"重试 5 次仍失败"。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)

        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        # 5 次连返 503
        respx.get(AUDIO_URL).mock(
            side_effect=[Response(503, text="down")] * 5,
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )

        with pytest.raises(MinimaxTtsError, match="audio_url.*重试 5 次仍失败"):
            await client.synthesize_segment("x", voice="male-qn-qingse")
        # base=1, cap=30, max=5 → 序列 1, 2, 4, 8；最后一次失败 break 不再 sleep
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_429_is_retryable(self, monkeypatch):
        """HTTP 429 → 走重试。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        respx.get(AUDIO_URL).mock(
            side_effect=[Response(429, text="rate limit"), Response(200, content=FAKE_MP3)],
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        result = await client.synthesize_segment("x", voice="male-qn-qingse")
        assert result.audio_bytes == FAKE_MP3
        assert sleeps == [1.0]
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_404_raises_immediately_no_retry(self, monkeypatch):
        """4xx 业务错立即 raise，不重试。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        respx.get(AUDIO_URL).mock(return_value=Response(404, text="not found"))
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        with pytest.raises(MinimaxTtsError, match="HTTP 404"):
            await client.synthesize_segment("x", voice="male-qn-qingse")
        # 4xx 不消耗 sleep 预算
        assert sleeps == []
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_403_raises_immediately_no_retry(self, monkeypatch):
        """403 业务错立即 raise。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        respx.get(AUDIO_URL).mock(return_value=Response(403, text="forbidden"))
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        with pytest.raises(MinimaxTtsError, match="HTTP 403"):
            await client.synthesize_segment("x", voice="male-qn-qingse")
        assert sleeps == []
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_network_timeout_raises_immediately(self, monkeypatch):
        """网络错（ConnectError）→ 仍然按计划会重试（属于网络白名单）。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)
        client = MinimaxTtsClient(_settings())
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        # 注意：respx 在某些场景下 ConnectError 会让 httpx 走 transient path；
        # 此处用 side_effect 抛 ConnectError 来精确测重试逻辑。
        import httpx as _httpx
        respx.get(AUDIO_URL).mock(
            side_effect=_httpx.ConnectError("dns fail"),
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )
        with pytest.raises(MinimaxTtsError, match="重试 5 次仍失败"):
            await client.synthesize_segment("x", voice="male-qn-qingse")
        # 网络错也走相同指数序列；最后一次失败 break 不再 sleep
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_backoff_caps_at_max_backoff_sec(self, monkeypatch):
        """base=10s cap=30s max=5 → sleep 序列 10, 20, 30, 30, 30。"""
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr("app.services.minimax_tts_provider.asyncio.sleep", fake_sleep)

        client = MinimaxTtsClient(_settings(
            url_fetch_initial_backoff_sec=10.0,
            url_fetch_max_backoff_sec=30.0,
        ))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio()),
        )
        respx.get(AUDIO_URL).mock(
            side_effect=[Response(503, text="x")] * 5,
        )
        respx.get(SUBTITLE_URL).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )

        with pytest.raises(MinimaxTtsError):
            await client.synthesize_segment("x", voice="male-qn-qingse")
        # 第 1 次: 10, 第 2 次: 20, 第 3 次: cap=30, 第 4 次: 30；第 5 次失败 break
        assert sleeps == [10.0, 20.0, 30.0, 30.0]
        await client.aclose()


# ---- v5 修复：OSS 下载 client 必须与 T2A client 头隔离 ---------------------


class TestDownloadClientIsolation:
    """v5 fix：OSS 预签名 URL 不能带 Authorization 头（会触发 SignatureDoesNotMatch）。

    respx 默认忽略实际请求头，本类显式校验 httpx client 的 headers 配置。
    """

    def test_t2a_client_has_auth_header_but_download_client_does_not(self):
        client = MinimaxTtsClient(_settings(api_key="sk-test"))
        t2a_headers = client._t2a_client.headers
        dl_headers = client._download_client.headers

        # T2A POST 必须带 Authorization Bearer + Content-Type: application/json
        assert "Authorization" in t2a_headers
        assert t2a_headers["Authorization"] == "Bearer sk-test"
        assert t2a_headers.get("Content-Type") == "application/json"

        # OSS GET client 必须不含 Authorization / Content-Type（httpx 默认
        # 的 Accept: */* / User-Agent / Accept-Encoding 等阿里云不纳入签名，可保留）
        assert "Authorization" not in dl_headers, (
            "OSS 预签名 URL 不能带 Authorization 头（阿里云 OSS 会因签名不匹配返 403），"
            f"但 download_client.headers = {dict(dl_headers)!r}"
        )
        assert "Content-Type" not in dl_headers
        # 关键安全断言：不得复用任何 Bearer 鉴权字串
        assert "Bearer" not in str(dl_headers)

    def test_two_clients_are_independent(self):
        """_download_with_retry 用的就是 _download_client，不是 _t2a_client。"""
        client = MinimaxTtsClient(_settings(api_key="sk-t"))
        assert client._download_client is not client._t2a_client


# ---- v5 字幕新形态：time_begin / time_end（毫秒）+ pronounce_text ------------


# 用户 2026-07-05 提供的真实 OSS .titles JSON 样本
REAL_OSS_TITLES_SAMPLE = [{
    "text": (
        "真正的危险不是计算机开始像人一样思考，而是人开始像计算机一样思考。"
        "计算机只是可以帮我们处理一些简单事务。"
    ),
    "pronounce_text": (
        "真正的危险不是计算机开始像人一样思考，而是人开始像计算机一样思考。"
        "计算机只是可以帮我们处理一些简单事务。"
    ),
    "time_begin": 0.0,
    "time_end": 9700.0,
    "text_begin": 0,
    "text_end": 52,
    "pronounce_text_begin": 0,
    "pronounce_text_end": 52,
    "is_final_segment": True,
}]


class TestSubtitleRealOssTitlesFormat:
    """v5 OSS 实测字幕形态：time_begin/time_end（毫秒）+ pronounce_text。"""

    def test_parses_real_oss_titles_format_with_time_begin_end_ms(self):
        """直接解析 user-provided 真实样本：ms → s。"""
        cues = MinimaxTtsClient._parse_subtitle_payload(REAL_OSS_TITLES_SAMPLE)
        assert len(cues) == 1
        # 毫秒 9700.0 → 秒 9.7
        assert cues[0][0] == 0.0
        assert cues[0][1] == 9.7
        assert cues[0][2] == REAL_OSS_TITLES_SAMPLE[0]["text"]

    def test_time_begin_millisecond_to_second_conversion(self):
        """单测：time_begin=0, time_end=5000 → (0.0, 5.0, ...)。"""
        payload = [{
            "text": "hello",
            "pronounce_text": "hello",
            "time_begin": 0,
            "time_end": 5000,
            "is_final_segment": True,
        }]
        cues = MinimaxTtsClient._parse_subtitle_payload(payload)
        assert cues == [(0.0, 5.0, "hello")]

    def test_time_begin_end_takes_priority_over_start_time_end_time(self):
        """time_begin/time_end 候选优先级比 start_time/end_time 高。"""
        payload = [{
            "text": "x",
            "time_begin": 100,        # 毫秒 → 0.1s
            "time_end": 1100,          # 毫秒 → 1.1s
            "start_time": 0,           # 不应被选
            "end_time": 9999,          # 不应被选
            "is_final_segment": True,
        }]
        cues = MinimaxTtsClient._parse_subtitle_payload(payload)
        assert cues == [(0.1, 1.1, "x")]

    def test_pronounce_text_fallback_when_text_missing(self):
        """没有 text 字段时回退到 pronounce_text。"""
        payload = [{
            "pronounce_text": "备用文本",
            "time_begin": 0,
            "time_end": 800,
        }]
        cues = MinimaxTtsClient._parse_subtitle_payload(payload)
        assert cues == [(0.0, 0.8, "备用文本")]


class TestSubtitleDownloadIsClean:
    """v5 fix：OSS 下载走 download_client，verify 实际请求不带 Authorization。"""

    @pytest.mark.asyncio
    @respx.mock
    async def test_url_download_request_does_not_carry_auth_header(self):
        """respx side_effect 拦下 GET，校验请求 headers 没有 Authorization。"""
        captured_headers = {}

        def capture_request(request):
            captured_headers.update(dict(request.headers))
            # 返回 mp3 bytes + 让 httpx 正确解码为二进制
            return Response(200, content=FAKE_MP3)

        # 不同 URL：audio 和 subtitle 各 mock 一份子路径，才不会让 respx
        # 把第二次请求当成对同一 URL 的复用挂掉。
        audio_url = "https://oss.example.com/audio/abc.mp3?token=A"
        subtitle_url = "https://oss.example.com/subtitles/abc.json?token=S"

        client = MinimaxTtsClient(_settings(api_key="sk-bearer"))
        respx.post("https://api.minimaxi.com/v1/t2a_v2").mock(
            return_value=Response(200, json=_t2a_ok_with_url_audio(
                audio_url=audio_url, subtitle_url=subtitle_url,
            )),
        )
        # audio: 返回 mp3 bytes
        respx.get(audio_url).mock(side_effect=capture_request)
        # subtitle: 返回字幕 JSON（避免第二个 respx.get 复用 audio 路由）
        respx.get(subtitle_url).mock(
            return_value=Response(200, json=_subtitle_json_sentence()),
        )

        await client.synthesize_segment("x", voice="male-qn-qingse")

        # 关键断言：OSS GET 请求里**不能**带 Authorization
        assert "authorization" not in {k.lower() for k in captured_headers}, (
            f"Authorization 不应在 OSS GET 请求里，实际 headers={captured_headers!r}"
        )
        # 同时也不应该含 Content-Type: application/json
        assert "content-type" not in {k.lower() for k in captured_headers}, (
            f"OSS GET 不应带 Content-Type: application/json（区分 POST），实际={captured_headers!r}"
        )
        await client.aclose()
