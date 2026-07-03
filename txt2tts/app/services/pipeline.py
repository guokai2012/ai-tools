"""Progress-emitting pipeline orchestrator.

The synthesize flow has 4 distinct stages, each with a clear start/end and
estimated weight. We expose them as an async generator that yields
``ProgressEvent`` objects the route can forward as Server-Sent Events.

Stage weights (sum to 1.0):
    0.00 - 0.10  markdown_clean   (very fast, local)
    0.10 - 0.35  llm_normalize    (MiniMax M3 round-trip ~ 2s)
    0.35 - 0.95  tts_synthesize   (Xiaomi MiMo / edge-tts round-trip)
    0.95 - 1.00  audio_save       (local disk write; edge 方案额外做 ffmpeg 合并)

The exact weights are UI hints only; stages are still gated on actual
completion, so the UI never advances before the next stage truly begins.

After a successful ``audio_save`` we also insert a metadata row into the
``LibraryStore`` SQLite index so the 「听文档」 feature can list and
replay the result.

方案二（edge-tts）下：
    llm_normalize  → semantic_preprocess（含多音字 [拼音] 标记、拆句）
    tts_synthesize → EdgeTtsClient 分段合成，输出每段 audio + 句级时间戳
    audio_save     → ffmpeg concat 所有片段，写 SRT 字幕文件 + LRC 歌词
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple

from app.config import PROVIDER_EDGE, PROVIDER_MIMO, EdgeTtsSettings, get_edge_voices
from app.services.audio_storage import AudioRecord, AudioStorageService, LibraryStore
from app.services.edge_tts_provider import (
    EdgeTtsClient,
    EdgeTtsError,
    ProviderResult,
    SegmentAudio,
    concat_segments_with_srt,
    format_lrc_timestamp,
    format_srt_timestamp,
    srt_to_lrc,
    split_sentences,
    strip_disambiguation,
)
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
    provider: Optional[str] = None  # "mimo" | "edge"，前端列表徽章用
    chunks_total: Optional[int] = None  # M3 切分后的子文档数量（仅 mimo provider）

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
        edge_tts: Optional[EdgeTtsClient] = None,
        ffmpeg_path: Optional[Path] = None,
        ffprobe_path: Optional[Path] = None,
        provider: str = PROVIDER_MIMO,
        edge_settings: Optional["EdgeTtsSettings"] = None,
    ) -> None:
        self._md = markdown
        self._llm = llm
        self._tts = tts
        self._audio = audio
        self._library = library  # may be None in legacy/test wiring
        self._edge_tts = edge_tts
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._provider = provider
        # edge ffmpeg/ffprobe 超时（env 可调）；可选，缺省时走 EdgeTtsSettings 默认值
        self._edge_settings = edge_settings
        # 后台任务包装时由 TaskManager 设置；用于 run() 末尾 promote_artifacts
        self._task_id: Optional[str] = None

    async def run(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
    ) -> AsyncIterator[ProgressEvent]:
        """Execute the full pipeline, yielding progress events."""
        provider = self._provider
        yield ProgressEvent(stage="start", progress=0.00,
                            message=f"开始处理…（provider={provider}）",
                            provider=provider)

        # ---- 1. markdown_clean ----
        lo, hi = self.STAGE_WEIGHTS["markdown_clean"]
        yield ProgressEvent(stage="markdown_clean", progress=lo,
                            message="本地 Markdown 清洗…",
                            provider=provider)
        try:
            markdown_text = self._decode(raw_bytes)
            local_clean = self._md.to_plain_text(markdown_text)
        except Exception as exc:
            logger.exception("markdown_clean failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="本地清洗失败", error=str(exc),
                                provider=provider)
            return
        if not local_clean.strip():
            yield ProgressEvent(stage="error", progress=lo,
                                message="Markdown 没有可读文本",
                                error="empty after cleaning",
                                provider=provider)
            return
        yield ProgressEvent(stage="markdown_clean", progress=hi,
                            message=f"本地清洗完成 · {len(local_clean)} 字符",
                            text_length=len(local_clean), provider=provider)

        # ---- 2. llm_normalize ----
        lo, hi = self.STAGE_WEIGHTS["llm_normalize"]
        if provider == PROVIDER_EDGE:
            msg = "M3 语义预处理（多音字 + 拆句）…"
        else:
            msg = "MiniMax M3 标准化中…"
        yield ProgressEvent(stage="llm_normalize", progress=lo, message=msg,
                            provider=provider)
        try:
            if provider == PROVIDER_EDGE:
                normalized = await self._llm.semantic_preprocess(local_clean)
            else:
                normalized = await self._llm.normalize(local_clean)
        except LlmNormalizationError as exc:
            logger.exception("llm_normalize failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="M3 标准化失败", error=str(exc),
                                provider=provider)
            return
        if not normalized.strip():
            yield ProgressEvent(stage="error", progress=lo,
                                message="M3 返回空文本", error="empty normalized",
                                provider=provider)
            return
        yield ProgressEvent(stage="llm_normalize", progress=hi,
                            message=f"M3 处理完成 · {len(normalized)} 字符",
                            text_length=len(normalized), provider=provider)

        # ---- 3. tts_synthesize ----
        lo, hi = self.STAGE_WEIGHTS["tts_synthesize"]
        if provider == PROVIDER_EDGE:
            msg = f"edge-tts 分段合成…"
        else:
            msg = "小米 MiMo 语音合成中…"
        yield ProgressEvent(stage="tts_synthesize", progress=lo,
                            message=msg, provider=provider)

        try:
            if provider == PROVIDER_EDGE:
                audio_bytes, srt_text = await self._run_edge_pipeline(normalized, voice_id)
            else:
                # mimo provider：M3 切分 → 持久化 → MiMo 分块 → 持久化 → ffmpeg 拼接
                audio_bytes, srt_text = await self._run_mimo_pipeline(
                    task_audio_id=None,  # 还未生成，下游由 audio_save 阶段补
                    normalized=normalized,
                    filename=filename,
                    voice_id=voice_id,
                )
        except (TtsApiError, EdgeTtsError, Exception) as exc:
            logger.exception("tts_synthesize failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="TTS 合成失败", error=str(exc),
                                provider=provider)
            return

        yield ProgressEvent(stage="tts_synthesize", progress=hi,
                            message=f"TTS 合成完成 · {len(audio_bytes)} bytes",
                            provider=provider)

        # ---- 4. audio_save ----
        lo, hi = self.STAGE_WEIGHTS["audio_save"]
        yield ProgressEvent(stage="audio_save", progress=lo,
                            message="保存音频到本地…", provider=provider)
        try:
            stored = self._audio.save(audio_bytes)
        except Exception as exc:
            logger.exception("audio_save failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="保存失败", error=str(exc),
                                provider=provider)
            return
        yield ProgressEvent(stage="audio_save", progress=hi,
                            message="保存完成", audio_id=stored.audio_id,
                            provider=provider)

        # ---- 4b. library index (听文档 metadata) -------------------------
        if self._library is not None:
            try:
                self._library.insert(AudioRecord(
                    audio_id=stored.audio_id,
                    original_filename=filename,
                    original_md=markdown_text,
                    normalized_md=normalized,
                    voice_id=voice_id or default_voice_id,
                    duration_sec=None,
                    byte_size=len(audio_bytes),
                    created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    provider=provider,
                ))
            except Exception:
                logger.exception("library insert failed for %s", stored.audio_id)

            # 方案二：保存 SRT 字幕 + LRC 歌词。统一写到 audio/_artifacts/<audio_id>/
            # 与最终 mp3 同址。**转歌词功能已移除**：这里 LRC 是 edge provider
            # 自身根据 SentenceBoundary cues 生成的字幕文件（同步时间戳），
            # **不**回写 library.lyrics_path（v2 之前由 LyricsService 单独跑
            # M3 二次改写，依赖 LLM 创作，已下线）。
            if provider == PROVIDER_EDGE and srt_text:
                try:
                    art_dir = self._audio.artifacts_dir(stored.audio_id)
                    art_dir.mkdir(parents=True, exist_ok=True)
                    srt_path = art_dir / f"{stored.audio_id}.srt"
                    srt_path.write_text(srt_text, encoding="utf-8")
                    from pathlib import Path as _P
                    title = _P(filename).stem or stored.audio_id[:8]
                    lrc_text = srt_to_lrc(srt_text, title=title, artist="txt2tts")
                    lrc_path = art_dir / f"{stored.audio_id}.lrc"
                    lrc_path.write_text(lrc_text, encoding="utf-8")
                    logger.info(
                        "edge provider: srt=%s, lrc=%s",
                        srt_path, lrc_path,
                    )
                except Exception:
                    logger.exception("failed to save SRT/LRC for %s", stored.audio_id)

        # ---- 4c. promote artifacts (成功任务的中间产物移入 audio/_artifacts/<audio_id>/) -----
        # 如果 pipeline 是从 TaskManager 包装而来（task_id 非空），
        # 任务成功意味着 uploader / chunks / segments 不再需要，
        # 全部搬进 audio/_artifacts/<audio_id>/ 集中管理。
        promote_task_id = getattr(self, "_task_id", None)
        if promote_task_id:
            try:
                self._audio.promote_artifacts(
                    task_id=promote_task_id,
                    audio_id=stored.audio_id,
                )
            except Exception:
                logger.exception(
                    "promote_artifacts failed for task_id=%s audio_id=%s",
                    promote_task_id, stored.audio_id,
                )

        # ---- done ----
        yield ProgressEvent(
            stage="done", progress=1.00, message="完成",
            audio_id=stored.audio_id,
            audio_url=f"/api/audio/{stored.audio_id}",
            voice_id=voice_id or default_voice_id,
            text_length=len(normalized),
            provider=provider,
        )

    # -- edge provider helpers ---------------------------------------------

    async def _run_edge_pipeline(
        self,
        normalized_text: str,
        voice_id: Optional[str],
    ) -> Tuple[bytes, str]:
        """方案二核心：分段合成 → 写每段 mp3 → ffprobe 取时长 → 生成 SRT → ffmpeg 合并。

        返回 (完整 mp3 bytes, 完整 SRT 文本)。

        关键点：``voice_id`` 是用户在上传对话框里选的（可能来自 MiMo 列表 /
        edge 列表 / 默认）。edge provider 只能接受 Microsoft edge-tts 的合法
        voice id（白名单见 ``app.config.EDGE_VOICES_ZH``）。如果传进来的
        voice 不在白名单，立即 fallback 到 ``EDGE__DEFAULT_VOICE``，避免
        把 MiMo voice id（例如 ``mimo_default``）透传给 edge-tts 触发
        ``Invalid voice 'mimo_default'`` 报错。
        """
        if self._edge_tts is None or self._ffmpeg_path is None:
            raise EdgeTtsError("EdgeTtsClient / ffmpeg 未配置。")

        # 1) edge voice 合法性校验 + fallback
        edge_voice = self._resolve_edge_voice(voice_id)

        # 2) 切句（剥离多音字标记）
        spoken_text = strip_disambiguation(normalized_text)
        sentences = split_sentences(spoken_text, max_chars=200)
        if not sentences:
            raise EdgeTtsError("M3 输出无法拆出有效句子。")

        # 3) 逐段合成（顺序执行，避免并发触发 edge-tts 限流）
        audio_root = self._audio._root  # type: ignore[attr-defined]
        seg_dir = audio_root / "segments"  # type: ignore[attr-defined]
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 先占位一个 stored id（基于预生成的 uuid），再把片段存到 segments/<task_id>/...
        import uuid
        temp_task_id = uuid.uuid4().hex[:12]
        sub_dir = seg_dir / temp_task_id
        sub_dir.mkdir(parents=True, exist_ok=True)

        seg_files: List[Path] = []
        cumulative_sec = 0.0
        srt_blocks: List[str] = []
        for i, sent in enumerate(sentences):
            if not sent.strip():
                continue
            audio_chunk, cues = await self._edge_tts.synthesize_segment(
                sent, voice=edge_voice,
            )
            seg_path = sub_dir / f"{i:04d}.mp3"
            seg_path.write_bytes(audio_chunk)
            seg_files.append(seg_path)
            # 句级时间戳 = 段起始 + 段内 offset
            for j, (off_s, _end_s, txt) in enumerate(cues):
                start = cumulative_sec + off_s
                srt_blocks.append(
                    f"{len(srt_blocks) + 1}\n"
                    f"{format_srt_timestamp(start)} --> "
                    f"{format_srt_timestamp(start + 0.5)}\n"
                    f"{txt.strip()}\n"
                )
            # 用 ffprobe 累加时长
            if self._ffprobe_path:
                from app.services.edge_tts_provider import probe_audio_duration
                d = probe_audio_duration(
                    seg_path, self._ffprobe_path,
                    timeout_sec=self._probe_timeout(),
                )
                cumulative_sec += d if d > 0 else (len(sent) * 0.08)  # 兜底估算
            else:
                cumulative_sec += len(sent) * 0.08
        srt_text = "\n".join(srt_blocks)

        # 3) ffmpeg 合并
        out_dir = audio_root / "edge"  # type: ignore[attr-defined]
        out_dir.mkdir(parents=True, exist_ok=True)
        # 用 uuid 命名，避免重复
        import uuid as _uuid
        final_id = _uuid.uuid4().hex
        final_path = out_dir / f"{final_id}.mp3"
        concat_segments_with_srt(
            segment_files=seg_files,
            srt_path=Path(),  # SRT 文本已经构造，不需要再写
            output_path=final_path,
            ffmpeg_path=self._ffmpeg_path,
            timeout_sec=self._edge_concat_timeout(),
        )
        audio_bytes = final_path.read_bytes()
        # 把 ffmpeg 合并后的文件也复制一份到 outputs/<日期>/<uuid>.mp3
        # 这样 AudioStorageService.save() 的路径解析仍然走原来的 outputs/
        # 简化：直接读出 bytes 让上层 save() 统一管理
        return audio_bytes, srt_text

    async def _run_mimo_pipeline(
        self,
        *,
        task_audio_id: Optional[str],
        normalized: str,
        filename: str,
        voice_id: Optional[str],
    ) -> Tuple[bytes, str]:
        """mimo provider 完整链路：M3 切分 → 持久化 → MiMo 分块 → 持久化 → ffmpeg 合并。

        1. 持久化 normalized.md（去重命名前缀为 chunks/ 上一级）
        2. M3 切分（语义保持）→ 输出 List[str] 子文档
        3. 持久化每个子文档到 outputs/chunks/<task_audio_id>/NNN.md
        4. 对每个子文档调 MiMo TTS（限 ≤ 4500 字符，service 内部不再 chunk）
        5. 持久化每个子音频到 outputs/chunks/<task_audio_id>/NNN.mp3
        6. ffmpeg concat 所有子 mp3 → outputs/chunks/<task_audio_id>/final.mp3
        7. 返回 final.mp3 的 bytes + 累积的 srt_text（空）

        Returns:
            (mp3_bytes, "") — srt_text 在 mimo provider 中本就没意义
        """
        # 临时 task_audio_id 目录：在 audio_save 阶段前用 uuid 占位
        import uuid as _uuid
        import subprocess
        import json as _json
        temp_id = task_audio_id or _uuid.uuid4().hex
        # chunks 目录独立于 AudioStorageService._root，以便单元测试可注入假 audio
        audio_root = getattr(self._audio, "_root", None) or Path("outputs")
        chunks_dir = audio_root / "chunks" / temp_id
        chunks_dir.mkdir(parents=True, exist_ok=True)

        # 1) 持久化 normalized.md
        norm_path = chunks_dir / "normalized.md"
        norm_path.write_text(normalized, encoding="utf-8")
        logger.info("mimo: saved normalized.md (%d chars) at %s", len(normalized), norm_path)

        # 2) M3 切分（仅当 normalized 超阈值才走 split_text）
        max_chars = self._tts._settings.max_input_chars_per_request  # 默认 4500
        if len(normalized) <= max_chars:
            logger.info(
                "mimo: short text (%d <= %d chars), skip M3 splitting",
                len(normalized), max_chars,
            )
            chunks = [normalized]
        else:
            chunks = await self._llm.split_text(normalized, max_chars=max_chars)
            logger.info(
                "mimo: M3 split into %d chunks (max %d chars each)",
                len(chunks), max_chars,
            )

        # 3) 持久化每个子文档
        for i, ch in enumerate(chunks, 1):
            (chunks_dir / f"{i:03d}.md").write_text(ch, encoding="utf-8")
        logger.info("mimo: saved %d chunk .md files at %s", len(chunks), chunks_dir)

        # 4) + 5) 逐段 MiMo + 持久化每段 mp3
        seg_files: List[Path] = []
        for i, ch in enumerate(chunks, 1):
            if not ch.strip():
                continue
            audio_chunk = await self._tts.synthesize(ch, voice=voice_id)
            seg_path = chunks_dir / f"{i:03d}.mp3"
            seg_path.write_bytes(audio_chunk)
            seg_files.append(seg_path)
            logger.info(
                "mimo: MiMo chunk %d/%d done (%d chars → %d bytes mp3)",
                i, len(chunks), len(ch), len(audio_chunk),
            )

        # 6) ffmpeg concat（即使只有 1 段也走一次 ffmpeg 规范化）
        if not seg_files:
            raise TtsApiError("mimo: no segments produced")
        final_path = chunks_dir / "final.mp3"
        self._ffmpeg_concat(seg_files, final_path)
        audio_bytes = final_path.read_bytes()
        logger.info(
            "mimo: ffmpeg concat done, final mp3 = %d bytes at %s",
            len(audio_bytes), final_path,
        )
        return audio_bytes, ""

    def _ffmpeg_concat(self, seg_files: List[Path], output_path: Path) -> None:
        """用 ffmpeg concat demuxer 拼接 mp3 文件到 output_path。

        超时从 ``EdgeTtsSettings.mimo_ffmpeg_concat_timeout_sec`` 读（env 可调）。
        """
        import subprocess
        if not self._ffmpeg_path:
            raise TtsApiError("mimo: ffmpeg_path not configured")
        # 写 concat list
        concat_list = output_path.parent / f"{output_path.stem}.concat.txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for seg in seg_files:
                safe = str(seg.resolve()).replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")
        try:
            result = subprocess.run(
                [
                    str(self._ffmpeg_path), "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy", str(output_path),
                ],
                check=True, capture_output=True, text=True, timeout=self._mimo_concat_timeout(),
            )
        except subprocess.CalledProcessError as exc:
            logger.error("mimo: ffmpeg concat failed: %s", exc.stderr)
            raise TtsApiError(f"ffmpeg concat failed: {exc.stderr[-300:]}") from exc
        finally:
            try:
                concat_list.unlink()
            except OSError:
                pass

    # -- env 化超时 helper -------------------------------------------------

    def _probe_timeout(self) -> float:
        """ffprobe 探测 mp3 时长的子进程超时（EDGE__FFPROBE_TIMEOUT_SEC）。"""
        if self._edge_settings is not None:
            return float(self._edge_settings.ffprobe_timeout_sec)
        # 兼容旧 wiring：直接拿 EdgeTtsClient 内置 settings
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.ffprobe_timeout_sec)
        return 10.0  # 兜底默认

    def _edge_concat_timeout(self) -> float:
        """edge provider ffmpeg 合并的子进程超时（EDGE__FFMPEG_CONCAT_TIMEOUT_SEC）。"""
        if self._edge_settings is not None:
            return float(self._edge_settings.ffmpeg_concat_timeout_sec)
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.ffmpeg_concat_timeout_sec)
        return 120.0  # 兜底默认

    def _mimo_concat_timeout(self) -> float:
        """mimo provider ffmpeg 合并的子进程超时（EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC）。"""
        if self._edge_settings is not None:
            return float(self._edge_settings.mimo_ffmpeg_concat_timeout_sec)
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.mimo_ffmpeg_concat_timeout_sec)
        return 600.0  # 兜底默认

    def _resolve_edge_voice(self, voice_id: Optional[str]) -> str:
        """决定 edge-tts 实际使用的 voice id。

        规则：
            * voice_id 在 ``EDGE_VOICES_ZH`` 白名单 → 原样使用；
            * 否则（包括 ``mimo_default`` 等 MiMo voice、None、空字符串） →
              fallback 到 ``EdgeTtsSettings.default_voice``（默认
              ``zh-CN-XiaoxiaoNeural``）；
            * 若白名单为空 / settings 缺失 → 兜底 ``zh-CN-XiaoxiaoNeural``。

        触发 fallback 时记一条 WARNING 方便排查用户选了 MiMo voice 又切到 edge
        这种典型误操作。
        """
        edge_ids = {v["id"] for v in get_edge_voices()}
        fallback = "zh-CN-XiaoxiaoNeural"
        if self._edge_tts is not None:
            try:
                fallback = self._edge_tts._settings.default_voice or fallback
            except Exception:
                pass
        if voice_id and voice_id in edge_ids:
            return voice_id
        if voice_id:
            logger.warning(
                "edge provider: voice_id=%r 不在 edge voice 白名单，"
                "fallback 到 %r",
                voice_id, fallback,
            )
        return fallback
        import subprocess
        if not self._ffmpeg_path:
            raise TtsApiError("mimo: ffmpeg_path not configured")
        # 写 concat list
        concat_list = output_path.parent / f"{output_path.stem}.concat.txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for seg in seg_files:
                safe = str(seg.resolve()).replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")
        try:
            result = subprocess.run(
                [
                    str(self._ffmpeg_path), "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy", str(output_path),
                ],
                check=True, capture_output=True, text=True, timeout=self._mimo_concat_timeout(),
            )
        except subprocess.CalledProcessError as exc:
            logger.error("mimo: ffmpeg concat failed: %s", exc.stderr)
            raise TtsApiError(f"ffmpeg concat failed: {exc.stderr[-300:]}") from exc
        finally:
            try:
                concat_list.unlink()
            except OSError:
                pass

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="ignore")