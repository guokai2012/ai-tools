"""Progress-emitting pipeline orchestrator.

The synthesize flow has 4 distinct stages, each with a clear start/end and
estimated weight. We expose them as an async generator that yields
``ProgressEvent`` objects the route can forward as Server-Sent Events.

Stage weights (sum to 1.0):
    0.00 - 0.10  markdown_clean   (very fast, local)
    0.10 - 0.35  llm_normalize    (MiniMax M3 round-trip ~ 2s)
    0.35 - 0.95  tts_synthesize   (Xiaomi MiMo round-trip ~ 10s)
    0.95 - 1.00  audio_save       (local disk write)

The exact weights are UI hints only; stages are still gated on actual
completion, so the UI never advances before the next stage truly begins.

After a successful ``audio_save`` we also insert a metadata row into the
``LibraryStore`` SQLite index so the 「听文档」 feature can list and
replay the result.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from app.services.audio_storage import AudioRecord, AudioStorageService, LibraryStore
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.tts_client import TtsApiError, TtsClient

logger = logging.getLogger(__name__)


# ---- Event types ----------------------------------------------------------


@dataclass
class ProgressEvent:
    """A single progress update emitted by the pipeline."""

    stage: str                # "markdown_clean" | "llm_normalize" | "tts_synthesize" | "audio_save" | "done" | "error"
    progress: float           # 0.0 .. 1.0
    message: str = ""         # human-readable status
    audio_id: Optional[str] = None
    audio_url: Optional[str] = None
    voice_id: Optional[str] = None
    text_length: Optional[int] = None
    error: Optional[str] = None

    def to_sse(self) -> str:
        """Render as a single ``data:`` line for SSE."""
        import json
        return "data: " + json.dumps(asdict(self), ensure_ascii=False) + "\n\n"


# ---- Pipeline -------------------------------------------------------------


class TtsPipeline:
    """Glue together the 4 services and emit progress events as it goes."""

    STAGE_WEIGHTS = {
        "start":          (0.00, 0.00),
        "markdown_clean": (0.00, 0.10),
        "llm_normalize":  (0.10, 0.35),
        "tts_synthesize": (0.35, 0.95),
        "audio_save":     (0.95, 1.00),
    }

    def __init__(
        self,
        markdown: MarkdownService,
        llm: LlmNormalizer,
        tts: TtsClient,
        audio: AudioStorageService,
        library: Optional[LibraryStore] = None,
    ) -> None:
        self._md = markdown
        self._llm = llm
        self._tts = tts
        self._audio = audio
        self._library = library  # may be None in legacy/test wiring

    async def run(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
    ) -> AsyncIterator[ProgressEvent]:
        """Execute the full pipeline, yielding progress events.

        Yields 4-6 ProgressEvent objects:
            - start
            - per-stage start (with that stage's beginning progress)
            - per-stage end (with that stage's ending progress)
            - done (with audio_url) OR error (with detail)
        """
        # ---- start ----
        yield ProgressEvent(stage="start", progress=0.00, message="开始处理…")

        # ---- 1. markdown_clean ----
        lo, hi = self.STAGE_WEIGHTS["markdown_clean"]
        yield ProgressEvent(stage="markdown_clean", progress=lo,
                            message="本地 Markdown 清洗…")
        try:
            markdown_text = self._decode(raw_bytes)
            local_clean = self._md.to_plain_text(markdown_text)
        except Exception as exc:  # decoding/parse failure
            logger.exception("markdown_clean failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="本地清洗失败",
                                error=str(exc))
            return
        if not local_clean.strip():
            yield ProgressEvent(stage="error", progress=lo,
                                message="Markdown 没有可读文本",
                                error="empty after cleaning")
            return
        yield ProgressEvent(stage="markdown_clean", progress=hi,
                            message=f"本地清洗完成 · {len(local_clean)} 字符",
                            text_length=len(local_clean))

        # ---- 2. llm_normalize ----
        lo, hi = self.STAGE_WEIGHTS["llm_normalize"]
        yield ProgressEvent(stage="llm_normalize", progress=lo,
                            message="MiniMax M3 标准化中…")
        try:
            normalized = await self._llm.normalize(local_clean)
        except LlmNormalizationError as exc:
            logger.exception("llm_normalize failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="M3 标准化失败",
                                error=str(exc))
            return
        if not normalized.strip():
            yield ProgressEvent(stage="error", progress=lo,
                                message="M3 返回空文本",
                                error="empty normalized")
            return
        yield ProgressEvent(stage="llm_normalize", progress=hi,
                            message=f"M3 标准化完成 · {len(normalized)} 字符",
                            text_length=len(normalized))

        # ---- 3. tts_synthesize ----
        lo, hi = self.STAGE_WEIGHTS["tts_synthesize"]
        yield ProgressEvent(stage="tts_synthesize", progress=lo,
                            message="小米 MiMo 语音合成中…")
        try:
            audio_bytes = await self._tts.synthesize(normalized, voice=voice_id)
        except TtsApiError as exc:
            logger.exception("tts_synthesize failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="TTS 合成失败",
                                error=str(exc))
            return
        # Audio arrived — burst to the end of the tts range so the UI feels snappy.
        yield ProgressEvent(stage="tts_synthesize", progress=hi,
                            message=f"TTS 合成完成 · {len(audio_bytes)} bytes")

        # ---- 4. audio_save ----
        lo, hi = self.STAGE_WEIGHTS["audio_save"]
        yield ProgressEvent(stage="audio_save", progress=lo,
                            message="保存音频到本地…")
        try:
            stored = self._audio.save(audio_bytes)
        except Exception as exc:
            logger.exception("audio_save failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="保存失败",
                                error=str(exc))
            return
        yield ProgressEvent(stage="audio_save", progress=hi,
                            message="保存完成",
                            audio_id=stored.audio_id)

        # ---- 4b. library index (听文档 metadata) -------------------------
        # Failures here are non-fatal: we don't want a broken index to abort
        # a successful synthesis. Log and move on.
        if self._library is not None:
            try:
                self._library.insert(AudioRecord(
                    audio_id=stored.audio_id,
                    original_filename=filename,
                    original_md=markdown_text,
                    normalized_md=normalized,
                    voice_id=voice_id or default_voice_id,
                    duration_sec=None,  # filled later by client / browser
                    byte_size=len(audio_bytes),
                    created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                ))
            except Exception:
                logger.exception("library insert failed for %s", stored.audio_id)

        # ---- done ----
        yield ProgressEvent(
            stage="done",
            progress=1.00,
            message="完成",
            audio_id=stored.audio_id,
            audio_url=f"/api/audio/{stored.audio_id}",
            voice_id=voice_id or default_voice_id,
            text_length=len(normalized),
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="ignore")