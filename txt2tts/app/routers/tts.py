"""HTTP routes for the txt2tts service.

任务模式 API:
    POST /api/tasks             -> 创建后台 TTS 转语音任务
    GET  /api/tasks             -> 分页列出任务
    GET  /api/tasks/{task_id}   -> 查询单条任务进度
    POST /api/preview           -> run M3 normalization, return JSON (no audio)

其他端点:
    GET  /api/voices             -> selectable voice list
    GET  /api/audio/{audio_id}   -> stream a stored mp3 (Range supported)
    GET  /api/health             -> liveness + key configuration status
    GET  /api/storage/stats      -> outputs/ size info
    GET  /api/library            -> 分页列出已完成的语音（听文档）
    GET  /api/library/{audio_id} -> 查询单条语音详情
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.config import (
    AppSettings,
    PROVIDERS,
    get_edge_voices,
    get_mimo_voices,
    get_settings,
)
from app.models.schemas import (
    AudioDetailDto,
    AudioRecordDto,
    LibraryPageDto,
    SettingsDto,
    SettingsUpdateDto,
    TaskCreateResponseDto,
    TaskDeleteResponseDto,
    TaskListDto,
    TaskRecordDto,
    VoiceDto,
    VoicesResponse,
)
from app.services.audio_storage import (
    AudioStorageService,
    LibraryStore,
    SettingsStore,
    TaskStore,
)
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.pipeline import TtsPipeline
from app.services.task_manager import TaskManager
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
_pipelines: dict = {}                # {"mimo": Pipeline, "edge": Pipeline}
_active_provider: str = "mimo"
_task_store: Optional[TaskStore] = None
_task_manager: Optional[TaskManager] = None
_settings_store: Optional[SettingsStore] = None


def configure(
    *,
    markdown: MarkdownService,
    llm: LlmNormalizer,
    tts: TtsClient,
    audio: AudioStorageService,
    library: LibraryStore,
    task_store: Optional[TaskStore] = None,
    settings_store: Optional[SettingsStore] = None,
    pipelines: Optional[dict] = None,
    active_provider: str = "mimo",
) -> None:
    """Inject the shared service instances. Called once from main.py."""
    global _markdown_svc, _llm_normalizer, _tts_client, _audio_storage, _library
    global _pipeline, _pipelines, _active_provider
    global _task_store, _task_manager, _settings_store
    _markdown_svc = markdown
    _llm_normalizer = llm
    _tts_client = tts
    _audio_storage = audio
    _library = library
    if pipelines:
        _pipelines = pipelines
        # 兼容老单 Pipeline 调用方：选 active 那条作为 _pipeline
        _pipeline = pipelines.get(active_provider) or pipelines.get("mimo")
    else:
        _pipeline = TtsPipeline(markdown=markdown, llm=llm, tts=tts, audio=audio, library=library)
        _pipelines = {"mimo": _pipeline}
    _active_provider = active_provider
    _task_store = task_store
    if task_store is not None:
        # 默认 uploads_dir 占位；create_task 路由会在收到上传时覆盖
        _task_manager = TaskManager(
            pipeline=_pipeline, task_store=task_store,
            uploads_dir=Path.cwd() / "uploads",
        )
    _settings_store = settings_store


def _active_pipeline() -> TtsPipeline:
    """根据运行时 active provider 返回当前 Pipeline；切换 provider 时 TaskManager 也要重建。"""
    global _task_manager, _pipeline
    pipe = _pipelines.get(_active_provider)
    if pipe is None:
        # 兜底：返回 mimo
        pipe = _pipelines.get("mimo")
    if pipe is None:
        raise HTTPException(status_code=503, detail="No pipeline available")
    # TaskManager 用最新 active pipeline
    if _task_manager is not None and _task_manager._pipeline is not pipe:
        _task_manager._pipeline = pipe  # type: ignore[attr-defined]
    _pipeline = pipe
    return pipe


def _set_active_provider(provider: str) -> None:
    """运行时切换 provider（写入 SettingsStore，更新 _active_provider）。"""
    global _active_provider
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    _active_provider = provider
    if _settings_store is not None:
        _settings_store.set("tts_provider", provider)


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


def _require_task_store() -> TaskStore:
    if _task_store is None:
        raise HTTPException(status_code=503, detail="TaskStore not initialized")
    return _task_store


def _require_task_manager() -> TaskManager:
    if _task_manager is None:
        raise HTTPException(status_code=503, detail="TaskManager not initialized")
    return _task_manager


def _require_settings_store() -> SettingsStore:
    if _settings_store is None:
        raise HTTPException(status_code=503, detail="SettingsStore not initialized")
    return _settings_store


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
async def voices(settings: AppSettings = Depends(get_settings)) -> VoicesResponse:
    # 当前 provider 是 edge 时优先返回 Edge voices；否则返回 MiMo 静态 voice
    if _active_provider == "edge":
        return VoicesResponse(
            voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                    for v in get_edge_voices()],
            source="edge",
        )
    return VoicesResponse(
        voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                for v in get_mimo_voices()],
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


@router.post("/tasks", response_model=TaskCreateResponseDto)
async def create_task(
    file: UploadFile = File(..., description="A .md (or .markdown/.txt) file"),
    voice_id: Optional[str] = Form(default=None),
    settings: AppSettings = Depends(get_settings),
) -> TaskCreateResponseDto:
    """上传 MD 文件并创建后台 TTS 转语音任务，返回 task_id 供前端轮询。"""
    # 校验上传文件
    filename = file.filename or "uploaded.md"
    lower = filename.lower()
    if not (lower.endswith(".md") or lower.endswith(".markdown") or lower.endswith(".txt")):
        raise HTTPException(status_code=400, detail="Only .md/.markdown/.txt files are supported.")
    raw = await file.read()
    if len(raw) > settings.max_md_size_kb * 1024:
        raise HTTPException(
            status_code=413, detail=f"File too large (>{settings.max_md_size_kb} KB)."
        )

    # 根据当前 active provider 选对应 pipeline
    if _task_store is None:
        raise HTTPException(status_code=503, detail="TaskStore not initialized")
    pipe = _active_pipeline()
    # 解析 uploads_dir = output_dir / uploads/（与 TaskManager 内部路径一致）
    from pathlib import Path as _Path
    uploads_dir = _Path(settings.output_dir).resolve() / "uploads"
    mgr = TaskManager(
        pipeline=pipe,
        task_store=_task_store,
        uploads_dir=uploads_dir,
        audio_storage=_require_audio(),
        library=_require_library(),
    )
    # 缓存为全局，避免每次重建
    global _task_manager
    _task_manager = mgr

    task_id = mgr.create_task(
        raw,
        filename=filename,
        voice_id=voice_id,
        default_voice_id=settings.tts.voice,
    )
    return TaskCreateResponseDto(task_id=task_id, message="任务已创建")


@router.post("/tasks/{task_id}/retry", response_model=TaskCreateResponseDto)
async def retry_task(task_id: str) -> TaskCreateResponseDto:
    """重试一个 failed / failed_retryable 任务。

    重新从 ``outputs/uploads/<task_id>.md`` 读出原始文件，走当前 active provider
    的 pipeline 全流程。retry_count 递增，error 列清空。
    """
    mgr = _require_task_manager()
    ok = mgr.retry_task(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        if record.status not in ("failed_retryable", "error"):
            raise HTTPException(
                status_code=409,
                detail=f"Task status={record.status!r} is not retryable.",
            )
        raise HTTPException(
            status_code=410,
            detail="原始 md 文件已丢失，无法重试。",
        )
    return TaskCreateResponseDto(task_id=task_id, message="已触发重试")


@router.delete("/tasks/{task_id}", response_model=TaskDeleteResponseDto)
async def delete_task(task_id: str) -> TaskDeleteResponseDto:
    """删除一个任务及其派生文件。

    行为：
        * ``status='done'`` 的任务会**保留**最终播放所需的
          ``outputs/audio/<audio_id>.mp3`` 与 ``outputs/audio/_artifacts/<audio_id>/``
          （包括 normalized.md / 子段 mp3 / srt / lrc / 原始 md），
          仅删除任务元数据 + 中间目录（uploads/chunks/segments）+ 听文档表行。
        * 其它状态（pending/processing/error/failed_retryable）的任务会**彻底**删除
          所有派生文件、库行、任务行。

    返回删除摘要（removed_files 各分类计数 + kept_final_audio 标志）。
    """
    mgr = _require_task_manager()
    if _task_store is None:
        raise HTTPException(status_code=503, detail="TaskStore not initialized")
    # 如果 lifespan 装的 manager 没带 audio_storage/library（兼容旧单测 wiring），
    # 这里补上，DELETE 完整清理需要这两个依赖。
    if mgr._audio_storage is None or mgr._library is None:
        from pathlib import Path as _Path
        settings = get_settings()
        uploads_dir = _Path(settings.output_dir).resolve() / "uploads"
        mgr = TaskManager(
            pipeline=mgr._pipeline,
            task_store=_task_store,
            uploads_dir=uploads_dir,
            audio_storage=_require_audio(),
            library=_require_library(),
        )
    result = mgr.delete_task(task_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Task not found.")
    return TaskDeleteResponseDto(**result)


@router.get("/tasks", response_model=TaskListDto)
async def list_tasks(
    page: int = 1,
    size: int = 20,
) -> TaskListDto:
    """分页列出后台转语音任务，按时间倒序。"""
    store = _require_task_store()
    page = max(1, page)
    size = max(1, min(size, 100))
    items, total = store.list_page(page=page, size=size)
    return TaskListDto(
        items=[
            TaskRecordDto(
                task_id=r.task_id,
                filename=r.filename,
                voice_id=r.voice_id,
                status=r.status,
                current_stage=r.current_stage,
                progress=r.progress,
                message=r.message,
                audio_id=r.audio_id,
                error=r.error,
                created_at=r.created_at,
                updated_at=r.updated_at,
                retry_count=r.retry_count,
                can_retry=(r.status == "failed_retryable"),
                provider=r.provider,
            )
            for r in items
        ],
        page=page,
        size=size,
        total=total,
    )


@router.get("/tasks/{task_id}", response_model=TaskRecordDto)
async def get_task(task_id: str) -> TaskRecordDto:
    """查询单条任务进度详情。"""
    store = _require_task_store()
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return TaskRecordDto(
        task_id=record.task_id,
        filename=record.filename,
        voice_id=record.voice_id,
        status=record.status,
        current_stage=record.current_stage,
        progress=record.progress,
        message=record.message,
        audio_id=record.audio_id,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        retry_count=record.retry_count,
        can_retry=(record.status == "failed_retryable"),
        provider=record.provider,
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
                has_lyrics=bool(r.lyrics_path),
                provider=r.provider or "mimo",
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
        lyrics_url=(f"/api/lyrics/{audio_id}.lrc" if record.lyrics_path else None),
    )


# ---- LRC 歌词文件下载（edge provider 自动落盘） -------------------------


@router.get("/lyrics/{filename}")
async def get_lyrics(filename: str) -> FileResponse:
    """下载 edge provider 在流水线里落盘的 LRC 字幕文件。

    **注意**：转歌词功能已移除（不再有 ``/api/library/{id}/lyrics`` 主动生成
    端点）。LRC 文件由 edge provider 自身在 ``run()`` 末尾根据 SentenceBoundary
    cues 写入 ``outputs/audio/_artifacts/<audio_id>/<audio_id>.lrc``，本端点
    只负责文件下载，供前端音乐播放器读取。
    """
    audio_svc = _require_audio()
    if "/" in filename or "\\" in filename or not filename.endswith(".lrc"):
        raise HTTPException(status_code=400, detail="Invalid lyrics filename.")
    # 仅允许 audio_id 形式的 hex + .lrc 命名
    stem = filename[:-4]
    if not stem or not all(c in "0123456789abcdef" for c in stem):
        raise HTTPException(status_code=400, detail="Invalid lyrics filename.")
    path = audio_svc.resolve_lyrics(stem)
    if path is None:
        raise HTTPException(status_code=404, detail="Lyrics file not found.")
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=filename,
        headers={"Cache-Control": "no-cache"},
    )


# ---- Settings -------------------------------------------------------------


@router.get("/settings", response_model=SettingsDto)
async def get_settings_api(settings: AppSettings = Depends(get_settings)) -> SettingsDto:
    """获取当前运行时设置（含可用 TTS provider 列表 + edge voices）。"""
    cur = _active_provider
    return SettingsDto(
        tts_provider=cur,
        tts_providers_available=list(PROVIDERS),
        edge_default_voice=settings.edge.default_voice,
        edge_voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                     for v in get_edge_voices()],
    )


@router.patch("/settings", response_model=SettingsDto)
async def update_settings_api(
    body: SettingsUpdateDto,
    settings: AppSettings = Depends(get_settings),
) -> SettingsDto:
    """更新运行时设置（目前支持 tts_provider 切换）。"""
    if body.tts_provider is not None:
        _set_active_provider(body.tts_provider)
        logger.info("TTS provider switched to: %s", body.tts_provider)
    cur = _active_provider
    return SettingsDto(
        tts_provider=cur,
        tts_providers_available=list(PROVIDERS),
        edge_default_voice=settings.edge.default_voice,
        edge_voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                     for v in get_edge_voices()],
    )
