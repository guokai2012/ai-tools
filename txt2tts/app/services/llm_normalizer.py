"""MiniMax M3 chat-completions client used to normalize Markdown-derived text
into a TTS-friendly form.

Pipeline position:
    MarkdownService.to_plain_text()  ->  LlmNormalizer.normalize()  ->  TtsClient.synthesize()

The normalizer is intentionally strict: if the M3 call fails for any reason
(timeouts, HTTP errors, malformed response, empty content), we raise
LlmNormalizationError. Per product decision, **no local fallback is applied**:
the route returns 502 to the client so the user is aware that the M3 step
did not complete successfully.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from app.config import LlmSettings, M3_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class LlmNormalizationError(RuntimeError):
    """Raised when the M3 normalization step fails."""


def _get_in(obj: Any, dotted: str) -> Optional[Any]:
    """Resolve a dotted response path like 'content.0.text' against a dict/list."""
    cur: Any = obj
    for part in dotted.split("."):
        if cur is None:
            return None
        if part.isdigit():
            idx = int(part)
            if isinstance(cur, list) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
    return cur


_FENCE_LINE_RE = re.compile(r"^\s*```", re.MULTILINE)
_OUTER_QUOTE_RE = re.compile(r'^[\s"\'`]+|[\s"\'`]+$')


class LlmNormalizer:
    """Async wrapper for a single M3 chat-completions call."""

    def __init__(self, settings: LlmSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_sec),
            headers={
                "x-api-key": settings.api_key,
                "anthropic-version": settings.api_version,
                "content-type": "application/json",
                "accept": "application/json",
            },
        )

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

        body = self._settings.build_request_body(
            system=system or M3_SYSTEM_PROMPT,
            user_text=text,
        )
        logger.debug(
            "M3 normalize request: url=%s model=%s text_len=%d",
            self._settings.messages_url, self._settings.model, len(text),
        )

        last_err: Optional[Exception] = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                resp = await self._client.post(
                    self._settings.messages_url, json=body
                )
            except httpx.HTTPError as exc:
                last_err = exc
                logger.warning("M3 HTTP error on attempt %d: %s", attempt + 1, exc)
                continue

            if resp.status_code >= 500:
                last_err = LlmNormalizationError(
                    f"M3 5xx: {resp.status_code} {resp.text[:200]}"
                )
                logger.warning("M3 5xx on attempt %d: %s", attempt + 1, last_err)
                continue

            if resp.status_code >= 400:
                raise LlmNormalizationError(
                    f"M3 API error {resp.status_code}: {resp.text[:500]}"
                )

            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                raise LlmNormalizationError(f"M3 returned non-JSON body: {exc}")

            content = _get_in(payload, self._settings.response_text_path)
            if not isinstance(content, str) or not content.strip():
                raise LlmNormalizationError(
                    f"M3 response missing text at '{self._settings.response_text_path}': "
                    f"{json.dumps(payload)[:300]}"
                )

            return self._post_process(content)

        raise LlmNormalizationError(f"M3 failed after retries: {last_err}")

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals -----------------------------------------------------------

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
                # If the first fence line was something like ```markdown, we already
                # removed it; nothing more to do.
                lines = inner_lines
        s = "\n".join(lines).strip()

        s = _OUTER_QUOTE_RE.sub("", s)
        return s.strip()