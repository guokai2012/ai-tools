"""Unit tests for LlmNormalizer.

M3 calls are mocked via `unittest.mock.patch` against `LlmNormalizer._get_client`,
which returns a fake ChatAnthropic client with a programmable `ainvoke`.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import LlmSettings, M3_SYSTEM_PROMPT
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(api_key: str = "test-key") -> LlmSettings:
    return LlmSettings(
        api_key=api_key,
        base_url="https://api.example.com/anthropic",
        model="MiniMax-M3",
        max_retries=0,
        request_timeout_sec=5.0,
    )


class _FakeResp:
    """Mimics the minimal shape of `langchain_core.messages.AIMessage`."""

    def __init__(self, content: str):
        self.content = content


def _patch_client(content: str | None = "标准化的中文文本。", side_effect: BaseException | None = None):
    """Patch `LlmNormalizer._get_client` to return a programmable fake client.

    Usage:
        with _patch_client(content="hi"):
            ... await n.normalize("x")
        with _patch_client(side_effect=RuntimeError("boom")):
            ... await n.normalize("x")
    """
    client = MagicMock()
    if side_effect is not None:
        client.ainvoke = AsyncMock(side_effect=side_effect)
    else:
        client.ainvoke = AsyncMock(return_value=_FakeResp(content or ""))
    return patch.object(LlmNormalizer, "_get_client", return_value=client)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


async def test_normalize_happy_path():
    n = LlmNormalizer(_settings())
    with _patch_client(content="标准化的中文文本。"):
        out = await n.normalize("原始文本")
    assert out == "标准化的中文文本。"


async def test_normalize_strips_code_fences():
    n = LlmNormalizer(_settings())
    with _patch_client(content="```\n干净的输出\n```"):
        out = await n.normalize("x")
    assert out == "干净的输出"


async def test_normalize_strips_code_fence_with_lang_tag():
    n = LlmNormalizer(_settings())
    with _patch_client(content="```markdown\n文本 A\n文本 B\n```"):
        out = await n.normalize("x")
    assert out == "文本 A\n文本 B"


async def test_normalize_strips_outer_quotes():
    n = LlmNormalizer(_settings())
    with _patch_client(content='"被引号包住的文本"'):
        out = await n.normalize("x")
    assert out == "被引号包住的文本"


async def test_normalize_sends_system_prompt():
    """The ChatAnthropic call must include the M3 system prompt + user text."""
    n = LlmNormalizer(_settings())
    client = MagicMock()
    client.ainvoke = AsyncMock(return_value=_FakeResp("ok"))
    with patch.object(LlmNormalizer, "_get_client", return_value=client):
        await n.normalize("hello world")

    client.ainvoke.assert_awaited_once()
    messages = client.ainvoke.await_args.args[0]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == M3_SYSTEM_PROMPT
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "hello world"


async def test_normalize_raises_on_5xx():
    """SDK/network errors are wrapped as LlmNormalizationError."""
    n = LlmNormalizer(_settings())
    with _patch_client(side_effect=RuntimeError("upstream busy")):
        with pytest.raises(LlmNormalizationError):
            await n.normalize("x")


async def test_normalize_raises_on_4xx_no_retry():
    n = LlmNormalizer(_settings())
    with _patch_client(side_effect=RuntimeError("401 unauthorized")):
        with pytest.raises(LlmNormalizationError) as exc:
            await n.normalize("x")
        # Original SDK error text should be present.
        assert "401" in str(exc.value) or "unauthorized" in str(exc.value)


async def test_normalize_raises_on_empty_response():
    n = LlmNormalizer(_settings())
    # SDK returned a message object but `.content` is empty.
    with _patch_client(content=""):
        with pytest.raises(LlmNormalizationError):
            await n.normalize("x")


async def test_normalize_requires_api_key():
    n = LlmNormalizer(_settings(api_key=""))
    with pytest.raises(LlmNormalizationError) as exc:
        await n.normalize("x")
    assert "API key" in str(exc.value)


async def test_normalize_rejects_empty_input():
    n = LlmNormalizer(_settings())
    with pytest.raises(LlmNormalizationError):
        await n.normalize("   \n\n  ")