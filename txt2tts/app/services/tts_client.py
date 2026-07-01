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

from app.config import StaticVoices, TtsSettings

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
        """Call MiMo TTS and return raw audio bytes (mp3 by default)."""
        if not text or not text.strip():
            raise TtsApiError("Refusing to synthesize empty text.")
        if not self._settings.api_key:
            raise TtsApiError(
                "Xiaomi MiMo API key is not configured. Set TTS__API_KEY env var."
            )

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

    async def list_voices(self) -> tuple[List[dict], str]:
        """Return the static MiMo voice list (the API does not expose a
        /v1/voices endpoint; the validated voice ids come from a 4xx error
        message we triggered during integration probing)."""
        return list(StaticVoices.items), "static"

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals -----------------------------------------------------------

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