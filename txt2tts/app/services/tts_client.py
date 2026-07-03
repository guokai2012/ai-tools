"""HTTP client for Xiaomi MiMo TTS (mimo-v2.5-tts).

The TTS endpoint is actually the chat-completions endpoint with multimodal
audio output. Verified flow:

    POST {base_url}/v1/chat/completions
    Headers: Authorization: Bearer <api_key>
    Body:
      {
        "model": "mimo-v2.5-tts",
        "modalities": ["text", "audio"],
        "audio": {"voice": "<voice>", "format": "mp3" | "wav"},
        "messages": [
          {"role":"user",     "content":"请朗读下面这段话：<text>"},
          {"role":"assistant","content":"<text>"}
        ]
      }
    Response:
      {
        "choices": [{
          "message": {
            "role": "assistant",
            "audio": {"id": "...", "data": "<base64>"},
            "content": ""
          }
        }]
      }

The base64 audio data is decoded into raw mp3/wav bytes and returned.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import List, Optional

import httpx

from app.config import TtsSettings, get_mimo_voices

logger = logging.getLogger(__name__)


class TtsApiError(RuntimeError):
    """Raised when the MiMo TTS endpoint returns an error or unusable body."""


class TtsClient:
    """Thin async wrapper around httpx for Xiaomi MiMo TTS."""

    def __init__(self, settings: TtsSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_sec),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.api_key}",
            },
        )

    # -- public API ----------------------------------------------------------

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """Call MiMo TTS and return raw audio bytes (mp3 by default).

        若文本超过 ``max_input_chars_per_request``（默认 6000 字符），会自动按
        句末标点切分为多段，**多次调用 MiMo 后拼接 mp3 bytes**。mp3 是流式
        容器，简单拼接在解码端无感知。
        """
        if not text or not text.strip():
            raise TtsApiError("Refusing to synthesize empty text.")
        if not self._settings.api_key:
            raise TtsApiError(
                "Xiaomi MiMo API key is not configured. Set TTS__API_KEY env var."
            )

        max_chars = self._settings.max_input_chars_per_request
        if max_chars <= 0:
            max_chars = 6000  # 兜底

        if len(text) <= max_chars:
            return await self._synthesize_one(text, voice=voice)

        # 长文本：按段落 / 句末标点切分
        chunks = self._split_into_chunks(text, max_chars=max_chars)
        logger.info(
            "MiMo TTS long text (%d chars) split into %d chunks (max %d/chunk)",
            len(text), len(chunks), max_chars,
        )
        results: List[bytes] = []
        for i, chunk in enumerate(chunks):
            logger.debug(
                "MiMo TTS chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk),
            )
            results.append(await self._synthesize_one(chunk, voice=voice))
        return b"".join(results)

    async def list_voices(self) -> tuple[List[dict], str]:
        """Return the static MiMo voice list (the API does not expose a
        /v1/voices endpoint; the validated voice ids come from a 4xx error
        message we triggered during integration probing)."""
        return list(get_mimo_voices()), "static"

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals -----------------------------------------------------------

    async def _synthesize_one(self, text: str, *, voice: Optional[str]) -> bytes:
        """对 MiMo 发起单次请求（含外层 max_retries）。返回 mp3 字节。"""
        body = self._settings.build_request_body(text=text, voice=voice)
        v = body.get("audio", {}).get("voice", "?")
        logger.debug(
            "MiMo TTS request url=%s model=%s voice=%s text_len=%d",
            self._settings.chat_url, self._settings.model, v, len(text),
        )

        last_err: Optional[Exception] = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                resp = await self._client.post(self._settings.chat_url, json=body)
            except httpx.HTTPError as exc:
                last_err = exc
                logger.warning("MiMo TTS HTTP error on attempt %d: %s", attempt + 1, exc)
                continue

            if resp.status_code >= 500:
                last_err = TtsApiError(f"MiMo 5xx: {resp.status_code} {resp.text[:200]}")
                logger.warning("MiMo TTS 5xx on attempt %d: %s", attempt + 1, last_err)
                continue

            if resp.status_code >= 400:
                raise TtsApiError(
                    f"MiMo API error {resp.status_code}: {resp.text[:500]}"
                )

            return self._extract_audio(resp)

        raise TtsApiError(f"MiMo TTS failed after retries: {last_err}")

    @staticmethod
    def _split_into_chunks(text: str, *, max_chars: int) -> List[str]:
        """按段落（空行）+ 句末标点切分长文本。每块不超过 max_chars。

        切分语义：
          - 输入先用 "\\n\\n" 拆为多个 paragraph（保留段落分隔信息，但不在 chunk 内
            重新插入 \n\n —— 因为 TTS 不需要段落停顿，纯文本更稳）。
          - 每个 paragraph 若 ≤ max_chars 直接加入累积 buf；否则按句末标点切碎。
          - 累积 buf + 下一个 paragraph 的总长超 max_chars 时 flush。
          - **兜底**：句末标点极少 / 无（纯 a 串）时，按 max_chars 硬切。
        """
        if len(text) <= max_chars:
            return [text]
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[str] = []
        current = ""
        import re
        for para in paragraphs:
            # 单段超长 → 按句末标点切（保留标点）
            if len(para) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                parts = re.split(r"(?<=[。！？!?\n])", para)
                buf = ""
                for piece in parts:
                    # 若标点切分后单 piece 仍超长 → 按 max_chars 硬切
                    while len(piece) > max_chars:
                        if buf:
                            chunks.append(buf)
                            buf = ""
                        chunks.append(piece[:max_chars])
                        piece = piece[max_chars:]
                    if len(buf) + len(piece) > max_chars and buf:
                        chunks.append(buf)
                        buf = piece
                    else:
                        buf += piece
                if buf:
                    chunks.append(buf)
                continue

            # 累积段落；若超 max_chars 就 flush
            if current and len(current) + 2 + len(para) > max_chars:
                chunks.append(current)
                current = para
            else:
                current = (current + "\n\n" + para) if current else para
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _extract_audio(resp: httpx.Response) -> bytes:
        """Decode the base64 audio payload from MiMo's response."""
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise TtsApiError(f"MiMo returned non-JSON body: {exc}")

        choices = payload.get("choices") or []
        if not choices:
            raise TtsApiError(f"MiMo response has no choices: {json.dumps(payload)[:300]}")
        message = choices[0].get("message") or {}
        audio = message.get("audio")
        if not isinstance(audio, dict):
            raise TtsApiError(
                f"MiMo response has no audio field: {json.dumps(payload)[:300]}"
            )
        b64 = audio.get("data")
        if not isinstance(b64, str) or not b64:
            raise TtsApiError("MiMo response audio.data is empty.")
        try:
            return base64.b64decode(b64)
        except (binascii.Error, ValueError) as exc:
            raise TtsApiError(f"Failed to decode MiMo audio.data: {exc}")


import binascii  # noqa: E402  (kept at bottom for clarity of code-flow)