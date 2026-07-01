"""Unit tests for LlmNormalizer. M3 HTTP is mocked via respx."""
import pytest
import respx
from httpx import Response

from app.config import LlmSettings, M3_SYSTEM_PROMPT
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer


pytestmark = pytest.mark.asyncio


def _settings(api_key: str = "test-key") -> LlmSettings:
    return LlmSettings(api_key=api_key, base_url="https://api.example.com/anthropic",
                       messages_path="/v1/messages", model="MiniMax-M3",
                       max_retries=0, request_timeout_sec=5.0)


def _anthropic_ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


@respx.mock
async def test_normalize_happy_path():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json=_anthropic_ok("标准化的中文文本。"))
    )
    try:
        out = await n.normalize("原始文本")
    finally:
        await n.aclose()
    assert out == "标准化的中文文本。"


@respx.mock
async def test_normalize_strips_code_fences():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json=_anthropic_ok("```\n干净的输出\n```"))
    )
    try:
        out = await n.normalize("x")
    finally:
        await n.aclose()
    assert out == "干净的输出"


@respx.mock
async def test_normalize_strips_code_fence_with_lang_tag():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json=_anthropic_ok("```markdown\n文本 A\n文本 B\n```"))
    )
    try:
        out = await n.normalize("x")
    finally:
        await n.aclose()
    assert out == "文本 A\n文本 B"


@respx.mock
async def test_normalize_strips_outer_quotes():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json=_anthropic_ok('"被引号包住的文本"'))
    )
    try:
        out = await n.normalize("x")
    finally:
        await n.aclose()
    assert out == "被引号包住的文本"


@respx.mock
async def test_normalize_sends_system_prompt():
    """The request body must include the M3 system prompt and the input text."""
    s = _settings()
    n = LlmNormalizer(s)
    route = respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json=_anthropic_ok("ok"))
    )
    try:
        await n.normalize("hello world")
    finally:
        await n.aclose()
    assert route.called
    body = route.calls.last.request.content
    import json as _json
    parsed = _json.loads(body)
    assert parsed["model"] == "MiniMax-M3"
    assert parsed["system"] == M3_SYSTEM_PROMPT
    assert parsed["messages"] == [{"role": "user", "content": "hello world"}]


@respx.mock
async def test_normalize_raises_on_5xx():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(503, text="upstream busy")
    )
    try:
        with pytest.raises(LlmNormalizationError):
            await n.normalize("x")
    finally:
        await n.aclose()


@respx.mock
async def test_normalize_raises_on_4xx_no_retry():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(401, text="bad key")
    )
    try:
        with pytest.raises(LlmNormalizationError) as exc:
            await n.normalize("x")
        assert "401" in str(exc.value)
    finally:
        await n.aclose()


@respx.mock
async def test_normalize_raises_on_empty_response():
    s = _settings()
    n = LlmNormalizer(s)
    respx.post("https://api.example.com/anthropic/v1/messages").mock(
        return_value=Response(200, json={"content": []})
    )
    try:
        with pytest.raises(LlmNormalizationError):
            await n.normalize("x")
    finally:
        await n.aclose()


async def test_normalize_requires_api_key():
    s = LlmSettings(api_key="", base_url="https://x", messages_path="/v1/messages")
    n = LlmNormalizer(s)
    try:
        with pytest.raises(LlmNormalizationError) as exc:
            await n.normalize("x")
        assert "API key" in str(exc.value)
    finally:
        await n.aclose()


async def test_normalize_rejects_empty_input():
    s = _settings()
    n = LlmNormalizer(s)
    try:
        with pytest.raises(LlmNormalizationError):
            await n.normalize("   \n\n  ")
    finally:
        await n.aclose()