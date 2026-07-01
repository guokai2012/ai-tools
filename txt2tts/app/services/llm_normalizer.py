"""MiniMax M3 normalization via LangChain ChatAnthropic.

Pipeline position:
    MarkdownService.to_plain_text()  ->  LlmNormalizer.normalize()  ->  TtsClient.synthesize()

The normalizer is intentionally strict: if the M3 call fails for any reason
(timeouts, HTTP errors, malformed response, empty content), we raise
LlmNormalizationError. Per product decision, **no local fallback is applied**:
the route returns 502 to the client so the user is aware that the M3 step
did not complete successfully.

HTTP-level concerns (auth headers, request body serialization, retry, response
parsing) are delegated to LangChain's `ChatAnthropic` SDK; we only orchestrate
the system / user message framing and our own outer retry loop.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import LlmSettings, M3_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class LlmNormalizationError(RuntimeError):
    """Raised when the M3 normalization step fails."""


_FENCE_LINE_RE = re.compile(r"^\s*```", re.MULTILINE)
_OUTER_QUOTE_RE = re.compile(r'^[\s"\'`]+|[\s"\'`]+$')


class LlmNormalizer:
    """Async wrapper for a single M3 ChatAnthropic call."""

    def __init__(self, settings: LlmSettings) -> None:
        self._settings = settings
        self._client: Optional[ChatAnthropic] = None  # lazy

    # -- public API ----------------------------------------------------------

    async def normalize(self, text: str, *, system: Optional[str] = None) -> str:
        """Send `text` to M3 and return its normalized reply.

        Raises LlmNormalizationError on any failure. The router turns this
        into an HTTP 502.
        """
        if not text or not text.strip():
            raise LlmNormalizationError("Refusing to normalize empty text.")
        if not self._settings.api_key:
            raise LlmNormalizationError(
                "MiniMax M3 API key is not configured. Set LLM__API_KEY env var."
            )

        client = self._get_client()
        messages = [
            SystemMessage(content=system or M3_SYSTEM_PROMPT),
            HumanMessage(content=text),
        ]
        logger.debug(
            "M3 normalize via ChatAnthropic: model=%s base=%s text_len=%d",
            self._settings.model, self._settings.base_url, len(text),
        )

        last_err: Optional[BaseException] = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                resp = await client.ainvoke(messages)
            except Exception as exc:  # 网络 / 5xx / SDK 错误
                last_err = exc
                logger.warning("M3 error on attempt %d: %s", attempt + 1, exc)
                continue

            content = getattr(resp, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise LlmNormalizationError(
                    f"M3 response missing text content: {repr(resp)[:300]}"
                )
            return self._post_process(content)

        raise LlmNormalizationError(f"M3 failed after retries: {last_err}")

    async def aclose(self) -> None:
        """Release resources. ChatAnthropic owns no explicit client; no-op."""
        return None

    # -- internals -----------------------------------------------------------

    def _get_client(self) -> ChatAnthropic:
        if self._client is None:
            self._client = ChatAnthropic(
                model=self._settings.model,
                api_key=self._settings.api_key,
                base_url=self._settings.base_url,
                max_tokens=self._settings.max_tokens,
                temperature=self._settings.temperature,
                timeout=self._settings.request_timeout_sec,
                max_retries=0,  # 外层 LlmNormalizer 独占重试，避免双层重试
            )
        return self._client

    @staticmethod
    def _post_process(content: str) -> str:
        """Strip accidental code fences and outer quotes the model may emit."""
        s = content.strip()
        lines = s.splitlines()

        # Find the first and last fence lines ``` … ```
        fence_idx = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
        if fence_idx:
            first = fence_idx[0]
            last = fence_idx[-1]
            if first != last:
                # Drop the first (which may carry a language tag) and the last fence.
                inner_lines = lines[first + 1:last]
                lines = inner_lines
        s = "\n".join(lines).strip()

        s = _OUTER_QUOTE_RE.sub("", s)
        return s.strip()