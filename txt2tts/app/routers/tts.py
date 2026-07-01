"""HTTP routes for the txt2tts service.

Pipeline streaming (SSE):
    POST /api/synthesize         -> text/event-stream, 5 progress events + done
    POST /api/preview            -> run M3 normalization, return JSON (no audio)

Other endpoints:
    GET  /api/voices             -> selectable voice list
    GET  /api/audio/{audio_id}   -> stream a stored mp3 (Range supported)
    GET  /api/health             -> liveness + key configuration status
    GET  /api/storage/stats      -> outputs/ size info
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.config import AppSettings, StaticVoices, get_settings
from app.models.schemas import (
    AudioDetailDto,
    AudioRecordDto,
    LibraryPageDto,
    SynthesizeResponse,
    VoiceDto,
    VoicesResponse,
)
from app.services.audio_storage import AudioStorageService, LibraryStore
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.pipeline import TtsPipeline
from app.services.tts_client import TtsApiError, TtsClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tts"])

# ---- shared service singletons (injected at startup) ----------------------

_markdown_svc: Optional[MarkdownService] = None
_llm_normalizer: Optional[LlmNormalizer] = None
_tts_client: Optional[TtsClient] = None
_audio_storage: Optional[AudioStorageService] = None
_library: Optional[LibraryStore] = None
_pipeline: Optional[TtsPipeline] = None


def configure(
    *,
    markdown: MarkdownService,
    llm: LlmNormalizer,
    tts: TtsClient,
    audio: AudioStorageService,
    library: LibraryStore,
) -> None:
    """Inject the shared service instances. Called once from main.py."""
    global _markdown_svc, _llm_normalizer, _tts_client, _audio_storage, _library, _pipeline
    _markdown_svc = markdown
    _llm_normalizer = llm
    _tts_client = tts
    _audio_storage = audio
    _library = library
    _pipeline = TtsPipeline(markdown=markdown, llm=llm, tts=tts, audio=audio, library=library)


def _require_md() -> MarkdownService:
    if _markdown_svc is None:
        raise HTTPException(status_code=503, detail="MarkdownService not initialized")
    return _markdown_svc


def _require_llm() -> LlmNormalizer:
    if _llm_normalizer is None:
        raise HTTPException(status_code=503, detail="LlmNormalizer not initialized")
    return _llm_normalizer


def _require_pipeline() -> TtsPipeline:
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="TtsPipeline not initialized")
    return _pipeline


def _require_audio() -> AudioStorageService:
    if _audio_storage is None:
        raise HTTPException(status_code=503, detail="AudioStorageService not initialized")
    return _audio_storage


def _require_library() -> LibraryStore:
    if _library is None:
        raise HTTPException(status_code=503, detail="LibraryStore not initialized")
    return _library


# ---- routes ----------------------------------------------------------------


@router.get("/health")
async def health(settings: AppSettings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "tts_configured": bool(settings.tts.api_key),
        "tts_model": settings.tts.model,
        "tts_base_url": settings.tts.base_url,
        "llm_configured": bool(settings.llm.api_key),
        "llm_model": settings.llm.model,
        "llm_base_url": settings.llm.base_url,
    }


@router.get("/voices", response_model=VoicesResponse)
async def voices() -> VoicesResponse:
    return VoicesResponse(
        voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                for v in StaticVoices.items],
        source="static",
    )


@router.post("/preview")
async def preview_markdown(
    file: UploadFile = File(...),
    settings: AppSettings = Depends(get_settings),
) -> JSONResponse:
    """Run local Markdown cleanup + M3 normalization and return both."""
    md_svc = _require_md()
    llm = _require_llm()

    raw = await file.read()
    if len(raw) > settings.max_md_size_kb * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (>{settings.max_md_size_kb} KB).",
        )
    try:
        markdown_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        markdown_text = raw.decode("gbk", errors="ignore")

    local_clean = md_svc.to_plain_text(markdown_text)
    if not local_clean.strip():
        raise HTTPException(status_code=422, detail="Markdown file has no readable text.")

    try:
        normalized = await llm.normalize(local_clean)
    except LlmNormalizationError as exc:
        logger.exception("M3 normalization failed during preview")
        raise HTTPException(status_code=502, detail=f"M3 normalization failed: {exc}")

    if len(normalized) > settings.max_normalized_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Normalized text too long ({len(normalized)} > {settings.max_normalized_chars}).",
        )

    return JSONResponse({
        "filename": file.filename,
        "local_clean": local_clean,
        "normalized": normalized,
        "length": len(normalized),
        "source": "llm",
    })


@router.post("/synthesize")
async def synthesize(
    file: UploadFile = File(..., description="A .md (or .markdown/.txt) file"),
    voice_id: Optional[str] = Form(default=None),
    settings: AppSettings = Depends(get_settings),
) -> StreamingResponse:
    """Stream the synthesize pipeline as Server-Sent Events.

    The response Content-Type is ``text/event-stream`` and the body is a
    sequence of ``data: {...}\\n\\n`` frames, each containing one
    ``ProgressEvent`` (see app/services/pipeline.py). The stream ends with
    either a ``done`` frame (containing ``audio_url``) or an ``error`` frame.
    """
    pipeline = _require_pipeline()

    # Validate the upload up front (cheap, before opening the stream).
    filename = file.filename or "uploaded.md"
    lower = filename.lower()
    if not (lower.endswith(".md") or lower.endswith(".markdown") or lower.endswith(".txt")):
        raise HTTPException(status_code=400, detail="Only .md/.markdown/.txt files are supported.")
    raw = await file.read()
    if len(raw) > settings.max_md_size_kb * 1024:
        raise HTTPException(
            status_code=413, detail=f"File too large (>{settings.max_md_size_kb} KB)."
        )

    async def event_source() -> AsyncIterator[bytes]:
        try:
            async for event in pipeline.run(
                raw,
                filename=filename,
                voice_id=voice_id,
                default_voice_id=settings.tts.voice,
            ):
                yield event.to_sse().encode("utf-8")
        except Exception as exc:
            logger.exception("pipeline.run raised")
            err = ProgressEvent(stage="error", progress=0.0,
                                message="pipeline crashed", error=str(exc))
            yield err.to_sse().encode("utf-8")

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


@router.get("/audio/{audio_id}")
async def get_audio(audio_id: str) -> FileResponse:
    audio_svc = _require_audio()
    path = audio_svc.resolve(audio_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Audio not found.")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"{audio_id}.mp3",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/storage/stats")
async def storage_stats() -> dict:
    return _require_audio().stats()


# ---- Library (听文档) ---------------------------------------------------


@router.get("/library", response_model=LibraryPageDto)
async def list_library(
    page: int = 1,
    size: int = 10,
) -> LibraryPageDto:
    """Paginated list of successfully synthesized audios, newest first."""
    library = _require_library()
    page = max(1, page)
    size = max(1, min(size, 100))  # cap page size
    items, total = library.list_page(page=page, size=size)
    return LibraryPageDto(
        items=[
            AudioRecordDto(
                audio_id=r.audio_id,
                original_filename=r.original_filename,
                voice_id=r.voice_id,
                duration_sec=r.duration_sec,
                byte_size=r.byte_size,
                created_at=r.created_at,
            )
            for r in items
        ],
        page=page,
        size=size,
        total=total,
    )


@router.get("/library/{audio_id}", response_model=AudioDetailDto)
async def get_library_item(audio_id: str) -> AudioDetailDto:
    """Detail view: includes the original + normalized Markdown for the player."""
    library = _require_library()
    record = library.get(audio_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Library entry not found.")
    return AudioDetailDto(
        audio_id=record.audio_id,
        original_filename=record.original_filename,
        original_md=record.original_md,
        normalized_md=record.normalized_md,
        voice_id=record.voice_id,
        duration_sec=record.duration_sec,
        byte_size=record.byte_size,
        created_at=record.created_at,
        audio_url=f"/api/audio/{record.audio_id}",
    )


# Local import to avoid polluting top-level namespace.
from app.services.pipeline import ProgressEvent  # noqa: E402  (used in error handler)