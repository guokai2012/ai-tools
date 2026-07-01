"""Unit tests for the Xiaomi MiMo TTS client (mocked via respx)."""
import base64
import pytest
import respx
from httpx import Response

from app.config import TtsSettings
from app.services.tts_client import TtsApiError, TtsClient


pytestmark = pytest.mark.asyncio


FAKE_MP3_BYTES = b"\xff\xfb\x90\x00" + b"\x00" * 1024  # > 1KB MP3 magic


def _settings(api_key: str = "test-key") -> TtsSettings:
    return TtsSettings(
        api_key=api_key,
        base_url="https://api.xiaomimimo.com",
        chat_path="/v1/chat/completions",
        model="mimo-v2.5-tts",
        voice="mimo_default",
        audio_format="mp3",
        max_retries=0,
        request_timeout_sec=5.0,
    )


def _mimo_reply(raw_audio: bytes) -> dict:
    return {
        "id": "test-reply-id",
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "audio": {
                        "id": "audio-id",
                        "data": base64.b64encode(raw_audio).decode("ascii"),
                    },
                },
            }
        ],
    }


@respx.mock
async def test_synthesize_happy_path():
    n = TtsClient(_settings())
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(200, json=_mimo_reply(FAKE_MP3_BYTES))
    )
    try:
        out = await n.synthesize("你好")
    finally:
        await n.aclose()
    assert out == FAKE_MP3_BYTES


@respx.mock
async def test_synthesize_uses_chat_completions_endpoint():
    n = TtsClient(_settings())
    route = respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(200, json=_mimo_reply(FAKE_MP3_BYTES))
    )
    try:
        await n.synthesize("hello")
    finally:
        await n.aclose()
    body = route.calls.last.request.content
    import json as _json
    parsed = _json.loads(body)
    # Anthropic-compat system prompt is gone; this is OpenAI multimodal chat.
    assert parsed["model"] == "mimo-v2.5-tts"
    assert parsed["modalities"] == ["text", "audio"]
    assert parsed["audio"]["voice"] == "mimo_default"
    assert parsed["audio"]["format"] == "mp3"
    # Two messages: user (frames it) + assistant (literal text to read).
    assert len(parsed["messages"]) == 2
    assert parsed["messages"][0]["role"] == "user"
    assert parsed["messages"][1]["role"] == "assistant"
    assert parsed["messages"][1]["content"] == "hello"


@respx.mock
async def test_synthesize_voice_override():
    n = TtsClient(_settings())
    route = respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(200, json=_mimo_reply(FAKE_MP3_BYTES))
    )
    try:
        await n.synthesize("x", voice="冰糖")
    finally:
        await n.aclose()
    import json as _json
    body = _json.loads(route.calls.last.request.content)
    assert body["audio"]["voice"] == "冰糖"


@respx.mock
async def test_synthesize_raises_on_empty_audio_data():
    n = TtsClient(_settings())
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": "", "audio": {"id": "x", "data": ""}}}
        ]
    }
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(200, json=payload)
    )
    try:
        with pytest.raises(TtsApiError) as exc:
            await n.synthesize("x")
        assert "audio.data" in str(exc.value) or "empty" in str(exc.value).lower()
    finally:
        await n.aclose()


@respx.mock
async def test_synthesize_raises_when_no_choices():
    n = TtsClient(_settings())
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": []})
    )
    try:
        with pytest.raises(TtsApiError):
            await n.synthesize("x")
    finally:
        await n.aclose()


@respx.mock
async def test_synthesize_raises_on_4xx():
    n = TtsClient(_settings())
    respx.post("https://api.xiaomimimo.com/v1/chat/completions").mock(
        return_value=Response(401, text="unauthorized")
    )
    try:
        with pytest.raises(TtsApiError) as exc:
            await n.synthesize("x")
        assert "401" in str(exc.value)
    finally:
        await n.aclose()


async def test_synthesize_requires_api_key():
    n = TtsClient(_settings(api_key=""))
    try:
        with pytest.raises(TtsApiError) as exc:
            await n.synthesize("x")
        assert "API key" in str(exc.value)
    finally:
        await n.aclose()


async def test_synthesize_rejects_empty_input():
    n = TtsClient(_settings())
    try:
        with pytest.raises(TtsApiError):
            await n.synthesize("   \n\n  ")
    finally:
        await n.aclose()


async def test_list_voices_returns_static():
    n = TtsClient(_settings())
    try:
        voices, source = await n.list_voices()
    finally:
        await n.aclose()
    assert source == "static"
    assert any(v["id"] == "mimo_default" for v in voices)
    assert any(v["id"] == "冰糖" for v in voices)