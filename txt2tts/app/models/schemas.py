"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class VoiceDto(BaseModel):
    id: str
    name: str
    lang: Optional[str] = None


class VoicesResponse(BaseModel):
    voices: List[VoiceDto]
    source: str = Field(description="'api' if fetched from MiniMax, 'static' otherwise")


class SynthesizeResponse(BaseModel):
    audio_id: str
    audio_url: str = Field(description="Relative URL to stream the mp3")
    duration_hint: Optional[float] = Field(
        default=None,
        description="Approximate duration in seconds, if known",
    )
    voice_id: str
    text_length: int


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None