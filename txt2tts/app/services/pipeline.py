"""Progress-emitting pipeline orchestrator.

The synthesize flow has 3 distinct stages, each with a clear start/end and
estimated weight. We expose them as an async generator that yields
``ProgressEvent`` objects the route can forward as Server-Sent Events.

Stage weights (sum to 1.0):
    0.00 - 0.35  llm_normalize    (MiniMax M3 round-trip ~ 2s)
    0.35 - 0.95  tts_synthesize   (MiniMax speech-2.8-hd / edge-tts round-trip)
    0.95 - 1.00  audio_save       (local disk write)

v5 起去掉了本地 markdown_clean 阶段 —— 原始 Markdown 一字不动地交给 M3，
让 M3 自行处理 # / * / > / 代码块 等格式。原先的 "本地清洗" 会过早抹掉
这些语义结构反而阻碍 M3 判断。

The exact weights are UI hints only; stages are still gated on actual
completion, so the UI never advances before the next stage truly begins.

v4 写盘布局：所有产物都写到 ``<output>/<yyyymmdd>/<task_id>/`` 下：
    normalized.md → split_<N>.md → split_<N>.mp3 + split_<N>.SRT
    → <task_id>.mp3 + <task_id>.SRT + <task_id>.LRC
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple

from app.config import (
    PROVIDER_EDGE,
    PROVIDER_MINIMAX,
    EdgeTtsSettings,
    get_edge_voices,
)
from app.services.audio_storage import AudioStorageService, task_date_str
from app.services.edge_tts_provider import (
    EdgeTtsClient,
    EdgeTtsError,
    concat_segments_with_srt,
    format_srt_timestamp,
    srt_to_lrc,
    split_sentences,
    strip_disambiguation,
)
from app.services.minimax_tts_provider import (
    MinimaxTtsClient,
    MinimaxTtsError,
    SubtitleFetchError,
)
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer

logger = logging.getLogger(__name__)


# ---- Event types ----------------------------------------------------------


@dataclass
class ProgressEvent:
    """A single progress update emitted by the pipeline."""

    stage: str                # "llm_normalize" | "tts_synthesize" | "audio_save" | "done" | "error"
    progress: float           # 0.0 .. 1.0
    message: str = ""         # human-readable status
    audio_id: Optional[str] = None  # v4 后已弃用；为兼容保留；值为 task_id
    audio_url: Optional[str] = None
    voice_id: Optional[str] = None
    text_length: Optional[int] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    chunks_total: Optional[int] = None
    subtitle_status: Optional[str] = None   # "ok" | "pending" | None
    subtitle_error: Optional[str] = None

    def to_sse(self) -> str:
        """Render as a single ``data:`` line for SSE."""
        import json
        return "data: " + json.dumps(asdict(self), ensure_ascii=False) + "\n\n"


# ---- Pipeline -------------------------------------------------------------


class TtsPipeline:
    """Glue together the services and emit progress events as it goes.

    v5 起去 ``markdown_clean`` 阶段，原始 MD 一字不动送进 M3。
    """

    STAGE_WEIGHTS = {
        "start":          (0.00, 0.00),
        "llm_normalize":  (0.00, 0.35),
        # v6：本地清洗是 TaskManager 在转换前同步完成的独立步骤，
        # pipeline.run_from_normalized 不会直接发射该事件；进度条权重
        # 预留 0.35→0.40 给 convert 前的 local_clean 阶段。事件本身由
        # TaskManager.update_progress 写入 current_stage='local_clean'。
        "local_clean":    (0.35, 0.40),
        "tts_synthesize": (0.40, 0.95),
        "audio_save":     (0.95, 1.00),
    }

    def __init__(
        self,
        llm: LlmNormalizer,
        audio: AudioStorageService,
        *,
        minimax_tts: Optional[MinimaxTtsClient] = None,
        edge_tts: Optional[EdgeTtsClient] = None,
        ffmpeg_path: Optional["Path"] = None,
        ffprobe_path: Optional["Path"] = None,
        provider: str = PROVIDER_MINIMAX,
        edge_settings: Optional[EdgeTtsSettings] = None,
    ) -> None:
        from pathlib import Path as _P  # noqa: F401
        self._llm = llm
        self._audio = audio
        self._minimax_tts = minimax_tts
        self._edge_tts = edge_tts
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._provider = provider
        self._edge_settings = edge_settings
        # 后台任务包装时由 TaskManager 设置；用于 task_dir() 路径
        self._task_id: Optional[str] = None
        # minimax provider 字幕拉取失败的诊断（pipeline 内部状态）
        self._subtitle_pending_error: Optional[str] = None
        # minimax provider 当次 run 用的 date_str（决定 task_dir 路径）
        self._task_date_str: Optional[str] = None

    async def run(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
    ) -> AsyncIterator[ProgressEvent]:
        """Execute the full pipeline, yielding progress events.

        v5 起：解码后的原始文本直接喂给 M3（不再做本地清洗）。
        """
        provider = self._provider
        yield ProgressEvent(stage="start", progress=0.00,
                            message=f"开始处理…（provider={provider}）",
                            provider=provider)

        raw_text = self._decode(raw_bytes)
        if not raw_text.strip():
            yield ProgressEvent(stage="error", progress=0.00,
                                message="文档无可读文本",
                                error="empty document",
                                provider=provider)
            return

        # ---- 1. llm_normalize（原始 MD 直接交 M3） ----
        lo, hi = self.STAGE_WEIGHTS["llm_normalize"]
        if provider == PROVIDER_EDGE:
            msg = "M3 语义预处理（多音字 + 拆句）…"
        else:
            msg = "MiniMax M3 标准化中…"
        yield ProgressEvent(stage="llm_normalize", progress=lo, message=msg,
                            provider=provider)
        try:
            if provider == PROVIDER_EDGE:
                normalized = await self._llm.semantic_preprocess(raw_text)
            else:
                normalized = await self._llm.normalize(raw_text)
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

        # ---- 2 + 3. tts_synthesize + audio_save 一体（pipeline.run 一站式跑完） ----
        async for evt in self._tts_stage_and_save(
            normalized, filename=filename,
            voice_id=voice_id, default_voice_id=default_voice_id,
        ):
            yield evt

    async def run_from_normalized(
        self,
        normalized: str,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
        pre_split_chunks: Optional[List[str]] = None,
    ) -> AsyncIterator[ProgressEvent]:
        """分步交互流程的 TTS 阶段入口（跳过 markdown_clean + llm_normalize）。"""
        provider = self._provider
        yield ProgressEvent(stage="start", progress=0.65,
                            message=f"开始 TTS 转换…（provider={provider}）",
                            provider=provider)

        if not normalized or not normalized.strip():
            yield ProgressEvent(stage="error", progress=0.65,
                                message="标准化文本为空，无法转换",
                                error="empty normalized",
                                provider=provider)
            return

        async for evt in self._tts_stage_and_save(
            normalized, filename=filename,
            voice_id=voice_id, default_voice_id=default_voice_id,
            pre_split_chunks=pre_split_chunks,
        ):
            yield evt

    # -- shared tts + save stage -------------------------------------------

    async def _tts_stage_and_save(
        self,
        normalized: str,
        *,
        filename: str,
        voice_id: Optional[str],
        default_voice_id: Optional[str],
        pre_split_chunks: Optional[List[str]] = None,
    ) -> AsyncIterator[ProgressEvent]:
        provider = self._provider

        # ---- 3. tts_synthesize ----
        lo, hi = self.STAGE_WEIGHTS["tts_synthesize"]
        if provider == PROVIDER_EDGE:
            msg = "edge-tts 分段合成…"
        else:
            msg = "MiniMax speech-2.8-hd 分块合成…"
        yield ProgressEvent(stage="tts_synthesize", progress=lo,
                            message=msg, provider=provider)

        try:
            if provider == PROVIDER_EDGE:
                audio_bytes, srt_text = await self._run_edge_pipeline(normalized, voice_id)
            else:
                audio_bytes, srt_text = await self._run_minimax_pipeline(
                    normalized=normalized,
                    filename=filename,
                    voice_id=voice_id,
                    pre_split_chunks=pre_split_chunks,
                )
        except (EdgeTtsError, MinimaxTtsError, Exception) as exc:
            logger.exception("tts_synthesize failed")
            yield ProgressEvent(stage="error", progress=lo,
                                message="TTS 合成失败", error=str(exc),
                                provider=provider)
            return

        yield ProgressEvent(stage="tts_synthesize", progress=hi,
                            message=f"TTS 合成完成 · {len(audio_bytes)} bytes",
                            provider=provider)

        # ---- 4. audio_save：写 <task_id>.mp3（task_dir/...） ----
        # 任务目录：<output>/<yyyymmdd>/<task_id>/（前提：TaskManager 已设 self._task_id）
        task_id = self._task_id
        if task_id:
            task_dir = self._audio.task_dir(task_id, date_str=self._task_date_str)
            mp3_path = task_dir / f"{task_id}.mp3"
            srt_path = task_dir / f"{task_id}.SRT"
            lrc_path = task_dir / f"{task_id}.LRC"
            lo, hi = self.STAGE_WEIGHTS["audio_save"]
            yield ProgressEvent(stage="audio_save", progress=lo,
                                message="保存音频到 task_dir…", provider=provider)
            try:
                mp3_path.write_bytes(audio_bytes)
                # 完整 SRT / LRC 落盘（如果 pipeline 已合成）
                if srt_text:
                    srt_path.write_text(srt_text, encoding="utf-8")
                    from app.services.edge_tts_provider import srt_to_lrc as _srt_to_lrc
                    lrc_text = _srt_to_lrc(srt_text, title=task_id[:8], artist="txt2tts")
                    lrc_path.write_text(lrc_text, encoding="utf-8")
            except Exception as exc:
                logger.exception("audio_save failed")
                yield ProgressEvent(stage="error", progress=lo,
                                    message="保存失败", error=str(exc),
                                    provider=provider)
                return
            subtitle_status = self._detect_subtitle_status(srt_text)
            yield ProgressEvent(stage="audio_save", progress=hi,
                                message=f"已保存到 {mp3_path}",
                                audio_id=task_id,
                                provider=provider,
                                subtitle_status=subtitle_status,
                                subtitle_error=getattr(self, "_subtitle_pending_error", None),
                                )
            yield ProgressEvent(
                stage="done", progress=1.00, message="完成",
                audio_id=task_id,
                audio_url=f"/api/audio/{task_id}",
                voice_id=voice_id or default_voice_id,
                text_length=len(normalized),
                provider=provider,
                subtitle_status=subtitle_status,
                subtitle_error=getattr(self, "_subtitle_pending_error", None),
            )
        else:
            # 没有 task_id（pipeline.run 入口走端到端测试/旧 e2e）：直接返回 mp3 bytes，不写文件
            lo, hi = self.STAGE_WEIGHTS["audio_save"]
            yield ProgressEvent(stage="audio_save", progress=lo,
                                message="无 task_id，跳过落盘", provider=provider)
            subtitle_status = self._detect_subtitle_status(srt_text)
            yield ProgressEvent(stage="audio_save", progress=hi,
                                message="TTS 完成（无 task_id 落盘）",
                                provider=provider)
            yield ProgressEvent(
                stage="done", progress=1.00, message="完成",
                voice_id=voice_id or default_voice_id,
                text_length=len(normalized),
                provider=provider,
                subtitle_status=subtitle_status,
                subtitle_error=getattr(self, "_subtitle_pending_error", None),
            )

    # -- minimax provider -------------------------------------------------

    async def _run_minimax_pipeline(
        self,
        *,
        normalized: str,
        filename: str,
        voice_id: Optional[str],
        pre_split_chunks: Optional[List[str]] = None,
    ) -> Tuple[bytes, str]:
        """MiniMax speech-2.8-hd 流水线。

        全部产物写到 ``task_dir/<task_id>/`` 下：
            normalized.md
            split_<N>.md / split_<N>.mp3 / split_<N>.SRT
            <task_id>.mp3 / <task_id>.SRT / <task_id>.LRC
        """
        if self._minimax_tts is None or self._ffmpeg_path is None:
            raise MinimaxTtsError("MinimaxTtsClient / ffmpeg 未配置。")

        self._subtitle_pending_error = None
        task_id = self._task_id or "pipeline_tmp"
        task_dir = self._audio.task_dir(task_id, date_str=self._task_date_str)

        # 1) 写 normalized.md
        try:
            (task_dir / "normalized.md").write_text(normalized, encoding="utf-8")
        except Exception:
            logger.exception("write normalized.md failed")

        # 2) 切分
        max_chars = self._minimax_tts._settings.max_input_chars_per_request
        if pre_split_chunks:
            chunks = [c for c in pre_split_chunks if c and c.strip()]
        elif len(normalized) <= max_chars:
            chunks = [normalized]
        else:
            chunks = await self._llm.split_text(normalized, max_chars=max_chars)
        if not chunks:
            raise MinimaxTtsError("minimax: 切分后无有效 chunk")

        # 写 split_<N>.md
        for i, ch in enumerate(chunks, 1):
            try:
                (task_dir / f"split_{i}.md").write_text(ch, encoding="utf-8")
            except Exception:
                logger.exception("write split_%d.md failed", i)

        # 3) 逐段合成
        cumulative_sec = 0.0
        all_cues: List[tuple] = []
        any_subtitle_pending = False
        first_subtitle_error: Optional[str] = None

        for i, ch in enumerate(chunks, 1):
            if not ch.strip():
                continue
            result = await self._minimax_tts.synthesize_segment(
                ch, voice=voice_id, title=Path(filename).stem,
            )
            # 写 split_<N>.mp3
            (task_dir / f"split_{i}.mp3").write_bytes(result.audio_bytes)
            # 写 split_<N>.SRT（minimax subtitle_file 解析得到的逐段 SRT）
            if result.srt_text:
                (task_dir / f"split_{i}.SRT").write_text(result.srt_text, encoding="utf-8")
            else:
                (task_dir / f"split_{i}.SRT").write_text("", encoding="utf-8")
            logger.info(
                "minimax: chunk %d/%d done (%d chars → %d bytes mp3, cues=%d)",
                i, len(chunks), len(ch), len(result.audio_bytes), len(result.sentence_cues),
            )
            if result.sentence_cues:
                for off_s, end_s, txt in result.sentence_cues:
                    all_cues.append((cumulative_sec + off_s, cumulative_sec + end_s, txt))
            elif result.subtitle_fetch_error:
                any_subtitle_pending = True
                if first_subtitle_error is None:
                    first_subtitle_error = result.subtitle_fetch_error
            cumulative_sec += max(result.duration_sec, 0.1)

        # 4) ffmpeg 合并 split_<N>.mp3 → <task_id>.mp3
        seg_files = sorted(task_dir.glob("split_*.mp3"))
        if not seg_files:
            raise MinimaxTtsError("minimax: no segments produced")
        final_path = task_dir / f"{task_id}.mp3"
        self._ffmpeg_concat(seg_files, final_path)
        audio_bytes = final_path.read_bytes()
        logger.info("minimax: ffmpeg concat done, %d bytes → %s", len(audio_bytes), final_path)

        # 5) 渲染累积偏移完整 SRT
        srt_text = self._render_srt_from_cues(all_cues) if all_cues else ""
        if any_subtitle_pending:
            self._subtitle_pending_error = first_subtitle_error or "subtitle_file 拉取失败"
            logger.warning(
                "minimax: 字幕拉取失败（含 %d cues 累计）: %s",
                len(all_cues), self._subtitle_pending_error,
            )
        return audio_bytes, srt_text

    @staticmethod
    def _render_srt_from_cues(cues: List[tuple]) -> str:
        if not cues:
            return ""
        blocks: List[str] = []
        for i, (start, end, text) in enumerate(cues, 1):
            blocks.append(
                f"{i}\n"
                f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n"
                f"{text.strip()}\n"
            )
        return "\n".join(blocks)

    # -- edge provider -----------------------------------------------------

    async def _run_edge_pipeline(
        self,
        normalized_text: str,
        voice_id: Optional[str],
    ) -> Tuple[bytes, str]:
        """方案二：edge-tts 分段 → ffmpeg 合并 → SRT 落盘 task_dir。"""
        if self._edge_tts is None or self._ffmpeg_path is None:
            raise EdgeTtsError("EdgeTtsClient / ffmpeg 未配置。")

        edge_voice = self._resolve_edge_voice(voice_id)
        spoken_text = strip_disambiguation(normalized_text)
        sentences = split_sentences(spoken_text, max_chars=200)
        if not sentences:
            raise EdgeTtsError("M3 输出无法拆出有效句子。")

        task_id = self._task_id or "pipeline_tmp"
        task_dir = self._audio.task_dir(task_id, date_str=self._task_date_str)
        seg_dir = task_dir  # edge 段也直接写在 task_dir（不再单独 segments/）
        seg_dir.mkdir(parents=True, exist_ok=True)

        seg_files: List[Path] = []
        cumulative_sec = 0.0
        srt_blocks: List[str] = []
        for i, sent in enumerate(sentences):
            if not sent.strip():
                continue
            audio_chunk, cues = await self._edge_tts.synthesize_segment(
                sent, voice=edge_voice,
            )
            seg_path = seg_dir / f"split_{i + 1:04d}.mp3"
            seg_path.write_bytes(audio_chunk)
            seg_files.append(seg_path)
            # 写 split_<N>.md
            try:
                (seg_dir / f"split_{i + 1:04d}.md").write_text(sent, encoding="utf-8")
            except Exception:
                pass
            # 写 split_<N>.SRT（逐段）
            block_lines = []
            for j, (off_s, _end_s, txt) in enumerate(cues):
                start = cumulative_sec + off_s
                block_lines.append(
                    f"{len(srt_blocks) + j + 1}\n"
                    f"{format_srt_timestamp(start)} --> "
                    f"{format_srt_timestamp(start + 0.5)}\n"
                    f"{txt.strip()}\n"
                )
            srt_blocks.extend(block_lines)
            try:
                (seg_dir / f"split_{i + 1:04d}.SRT").write_text(
                    "\n".join(block_lines) + "\n", encoding="utf-8",
                )
            except Exception:
                pass
            # 累计时长
            if self._ffprobe_path:
                from app.services.edge_tts_provider import probe_audio_duration
                d = probe_audio_duration(
                    seg_path, self._ffprobe_path,
                    timeout_sec=self._probe_timeout(),
                )
                cumulative_sec += d if d > 0 else (len(sent) * 0.08)
            else:
                cumulative_sec += len(sent) * 0.08

        srt_text = "\n".join(srt_blocks)

        # ffmpeg 合并
        final_path = task_dir / f"{task_id}.mp3"
        concat_segments_with_srt(
            segment_files=seg_files,
            srt_path=Path(),  # SRT 已构造，不重复
            output_path=final_path,
            ffmpeg_path=self._ffmpeg_path,
            timeout_sec=self._edge_concat_timeout(),
        )
        audio_bytes = final_path.read_bytes()
        return audio_bytes, srt_text

    # -- shared ffmpeg concat ---------------------------------------------

    def _ffmpeg_concat(self, seg_files: List[Path], output_path: Path) -> None:
        """用 ffmpeg concat demuxer 拼接 mp3 文件到 output_path。"""
        import subprocess
        if not self._ffmpeg_path:
            raise MinimaxTtsError("minimax: ffmpeg_path not configured")
        concat_list = output_path.parent / f"{output_path.stem}.concat.txt"
        with open(concat_list, "w", encoding="utf-8") as fh:
            for seg in seg_files:
                safe = str(seg.resolve()).replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")
        try:
            subprocess.run(
                [
                    str(self._ffmpeg_path), "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy", str(output_path),
                ],
                check=True, capture_output=True, text=True, timeout=self._minimax_concat_timeout(),
            )
        except subprocess.CalledProcessError as exc:
            logger.error("ffmpeg concat failed: %s", exc.stderr)
            raise MinimaxTtsError(f"ffmpeg concat failed: {exc.stderr[-300:]}") from exc
        finally:
            try:
                concat_list.unlink()
            except OSError:
                pass

    # -- env 化超时 helper -------------------------------------------------

    def _probe_timeout(self) -> float:
        if self._edge_settings is not None:
            return float(self._edge_settings.ffprobe_timeout_sec)
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.ffprobe_timeout_sec)
        return 10.0

    def _edge_concat_timeout(self) -> float:
        if self._edge_settings is not None:
            return float(self._edge_settings.ffmpeg_concat_timeout_sec)
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.ffmpeg_concat_timeout_sec)
        return 120.0

    def _minimax_concat_timeout(self) -> float:
        if self._edge_settings is not None:
            return float(self._edge_settings.mimo_ffmpeg_concat_timeout_sec)
        if self._edge_tts is not None:
            return float(self._edge_tts._settings.mimo_ffmpeg_concat_timeout_sec)
        return 600.0

    def _resolve_edge_voice(self, voice_id: Optional[str]) -> str:
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
                "edge provider: voice_id=%r 不在 edge voice 白名单，fallback 到 %r",
                voice_id, fallback,
            )
        return fallback

    # -- subtitle status ---------------------------------------------------

    def _detect_subtitle_status(self, srt_text: str) -> Optional[str]:
        err = getattr(self, "_subtitle_pending_error", None)
        if err:
            return "pending"
        if self._provider == PROVIDER_MINIMAX:
            return "ok" if srt_text else None
        return None

    # -- utils -------------------------------------------------------------

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="ignore")