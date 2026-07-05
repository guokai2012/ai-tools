"""HTTP routes for the txt2tts service (v6: 加本地清洗步骤).

任务模式 API（分步交互式 v6）：
    POST   /api/tasks                         -> 创建草稿任务（写 task_dir/<task_id>.md）
    GET    /api/tasks                         -> 分页列出任务
    GET    /api/tasks/{task_id}               -> 查询单条任务详情
    POST   /api/tasks/{task_id}/normalize     -> 触发 M3 标准化（写 task_dir/normalization.md）
    POST   /api/tasks/{task_id}/skip-normalize-> 跳过标准化（复制 <task_id>.md → normalization.md）
    POST   /api/tasks/{task_id}/split         -> 触发 M3 拆分（写 task_dir/split_<N>.md）
    POST   /api/tasks/{task_id}/confirm-split -> 确认子文档
    POST   /api/tasks/{task_id}/skip-split    -> 跳过拆分（复制 normalization.md → split_1.md）
    POST   /api/tasks/{task_id}/local-clean   -> v6 本地清洗（覆写 split_<N>.md）
    POST   /api/tasks/{task_id}/skip-local-clean -> v6 跳过本地清洗
    POST   /api/tasks/{task_id}/convert       -> 启动 TTS 转换（写 task_dir/{split,final} 产物）
    POST   /api/tasks/{task_id}/retry         -> 阶段感知重试
    DELETE /api/tasks/{task_id}               -> 删除任务（rmtree task_dir + 删 tasks 行）
    GET    /api/split-presets                 -> 内置拆分提示词
    GET    /api/normalize-presets             -> v6 内置标准化提示词
    GET    /api/clean-options                 -> v6 本地清洗项元数据
    POST   /api/preview                       -> run M3 normalization, return JSON (no audio)

其他端点:
    GET  /api/voices             -> selectable voice list
    GET  /api/audio/{task_id}    -> stream task_dir/<yyyymmdd>/<task_id>/<task_id>.mp3
    GET  /api/health             -> liveness + key configuration status
    GET  /api/storage/stats      -> outputs/ size info
    GET  /api/library            -> status='done' 任务列表（听文档）
    GET  /api/library/{task_id}  -> 单条任务详情（task_dir 文件系统拼装）
    GET  /api/lyrics/{task_id}.lrc -> 读 task_dir/<task_id>.LRC
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.config import (
    AppSettings,
    NORMALIZE_PRESETS,
    PROVIDERS,
    SPLIT_PRESETS,
    get_edge_voices,
    get_m3_system_prompt,
    get_minimax_voices,
    get_settings,
)
from app.models.schemas import (
    CleanOptionDto,
    ConfirmSplitRequest,
    LibraryDetailDto,
    LibraryItemDto,
    LibraryPageDto,
    LocalCleanRequest,
    NormalizeRequest,
    SettingsDto,
    SettingsUpdateDto,
    SplitPresetDto,
    SplitRequest,
    TaskCreateResponseDto,
    TaskDeleteResponseDto,
    TaskListDto,
    TaskRecordDto,
    VoiceDto,
    VoicesResponse,
)
from app.services.audio_storage import (
    TASK_RETRYABLE_STATUSES,
    TASK_STATUS_DONE,
    AudioStorageService,
    SettingsStore,
    TaskStore,
)
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.pipeline import TtsPipeline
from app.services.task_manager import TaskManager
from app.services.text_cleaner import get_clean_options as _get_clean_options

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tts"])

# ---- shared service singletons (injected at startup) ----------------------

_llm_normalizer: Optional[LlmNormalizer] = None
_audio_storage: Optional[AudioStorageService] = None
_pipeline: Optional[TtsPipeline] = None
_pipelines: dict = {}                # {"minimax": Pipeline, "edge": Pipeline}
_active_provider: str = "minimax"
_task_store: Optional[TaskStore] = None
_task_manager: Optional[TaskManager] = None
_settings_store: Optional[SettingsStore] = None


def configure(
    *,
    llm: LlmNormalizer,
    audio: AudioStorageService,
    task_store: Optional[TaskStore] = None,
    settings_store: Optional[SettingsStore] = None,
    pipelines: Optional[dict] = None,
    active_provider: str = "minimax",
) -> None:
    """Inject the shared service instances. Called once from main.py."""
    global _llm_normalizer, _audio_storage
    global _pipeline, _pipelines, _active_provider
    global _task_store, _task_manager, _settings_store
    _llm_normalizer = llm
    _audio_storage = audio
    if pipelines:
        _pipelines = pipelines
        _pipeline = pipelines.get(active_provider) or pipelines.get("minimax")
    else:
        _pipeline = None
        _pipelines = {}
    _active_provider = active_provider
    _task_store = task_store
    if task_store is not None and _pipeline is not None:
        _task_manager = TaskManager(
            pipeline=_pipeline, task_store=task_store,
            audio_storage=audio,
            llm=llm,
        )
    _settings_store = settings_store


def _active_pipeline() -> TtsPipeline:
    global _task_manager, _pipeline
    pipe = _pipelines.get(_active_provider)
    if pipe is None:
        pipe = _pipelines.get("minimax")
    if pipe is None:
        raise HTTPException(status_code=503, detail="No pipeline available")
    if _task_manager is not None and _task_manager._pipeline is not pipe:
        _task_manager._pipeline = pipe
    _pipeline = pipe
    return pipe


def _set_active_provider(provider: str) -> None:
    global _active_provider
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    _active_provider = provider
    if _settings_store is not None:
        _settings_store.set("tts_provider", provider)


def _require_md_removed() -> None:
    """v5 起本地 Markdown 清洗已删除。

    旧路由（preview）原本依赖 MarkdownService，现保留路径但返回 410 提示用户
    删除入口。如需保留 preview 行为可直接调 normalize —— 不走本地清洗版本。
    """
    raise HTTPException(
        status_code=410,
        detail="本地 Markdown 清洗已废弃（v5 起原始 MD 直接送 M3）。",
    )


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
        "tts_configured": bool(settings.minimax.api_key or settings.llm.api_key),
        "tts_model": settings.minimax.model,
        "tts_base_url": settings.minimax.base_url,
        "llm_configured": bool(settings.llm.api_key),
        "llm_model": settings.llm.model,
        "llm_base_url": settings.llm.base_url,
    }


@router.get("/voices", response_model=VoicesResponse)
async def voices(settings: AppSettings = Depends(get_settings)) -> VoicesResponse:
    if _active_provider == "edge":
        return VoicesResponse(
            voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                    for v in get_edge_voices()],
            source="edge",
        )
    return VoicesResponse(
        voices=[VoiceDto(id=v["id"], name=v["name"], lang=v.get("lang"))
                for v in get_minimax_voices()],
        source="static",
    )


@router.post("/preview")
async def preview_markdown(
    file: UploadFile = File(...),
    settings: AppSettings = Depends(get_settings),
) -> JSONResponse:
    """v5 起：本地 Markdown 清洗已废弃 —— 直接把原文 MD 送 M3 标准化。

    兼容旧 UI 字段（仍返回 local_clean 关键字，但内容 = 原文），不再做
    二次正则清洗；M3 自己负责处理 markdown 残留。
    """
    llm = _require_llm()

    raw = await file.read()
    if len(raw) > settings.max_md_size_kb * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{settings.max_md_size_kb} KB).")
    try:
        markdown_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        markdown_text = raw.decode("gbk", errors="ignore")

    if not markdown_text.strip():
        raise HTTPException(status_code=422, detail="Markdown file has no readable text.")

    try:
        normalized = await llm.normalize(markdown_text)
    except LlmNormalizationError as exc:
        logger.exception("M3 normalization failed during preview")
        raise HTTPException(status_code=502, detail=f"M3 normalization failed: {exc}")

    if len(normalized) > settings.max_normalized_chars:
        raise HTTPException(status_code=413, detail=f"Normalized text too long.")

    return JSONResponse({
        "filename": file.filename,
        # v5 起 local_clean 字段保留为原文（向后兼容旧前端），不再清洗
        "local_clean": markdown_text,
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
    """上传 MD 文件并创建后台 TTS 转语音任务，返回 task_id 供前端轮询。

    v5 起：原文 MD 直接落盘 + 入库 draft（不再做本地清洗）。
    """
    filename = file.filename or "uploaded.md"
    lower = filename.lower()
    if not (lower.endswith(".md") or lower.endswith(".markdown") or lower.endswith(".txt")):
        raise HTTPException(status_code=400, detail="Only .md/.markdown/.txt files are supported.")
    raw = await file.read()
    if len(raw) > settings.max_md_size_kb * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (>{settings.max_md_size_kb} KB).")

    if _task_store is None:
        raise HTTPException(status_code=503, detail="TaskStore not initialized")
    pipe = _active_pipeline()
    mgr = TaskManager(
        pipeline=pipe,
        task_store=_task_store,
        audio_storage=_require_audio(),
        llm=_require_llm(),
    )
    global _task_manager
    _task_manager = mgr

    task_id = mgr.create_task(
        raw,
        filename=filename,
        voice_id=voice_id,
        default_voice_id=settings.minimax.voice_id,
    )
    return TaskCreateResponseDto(task_id=task_id, message="草稿任务已创建")


@router.post("/tasks/{task_id}/retry", response_model=TaskCreateResponseDto)
async def retry_task(task_id: str) -> TaskCreateResponseDto:
    """阶段感知重试（v4）。"""
    mgr = _require_task_manager()
    ok = mgr.retry_task(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        if record.status not in TASK_RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Task status={record.status!r} is not retryable.",
            )
        raise HTTPException(
            status_code=410,
            detail="任务记录缺少必要字段或原始 md 已丢失，无法重试。",
        )
    return TaskCreateResponseDto(task_id=task_id, message="已触发重试")


# ---- 分步交互流程端点 ---------------------------------------------------


@router.post("/tasks/{task_id}/normalize", response_model=TaskCreateResponseDto)
async def normalize_task(
    task_id: str,
    body: Optional[NormalizeRequest] = None,
) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    # body.prompt 为空字符串 / None → 用 get_m3_system_prompt() 默认值
    prompt = body.prompt if body and body.prompt and body.prompt.strip() else None
    ok = mgr.normalize_task(task_id, system_prompt=prompt)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 draft 可标准化）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="M3 标准化已启动")


@router.post("/tasks/{task_id}/skip-normalize", response_model=TaskCreateResponseDto)
async def skip_normalize_task(task_id: str) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    ok = mgr.skip_normalize(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 draft 可跳过标准化）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="已跳过标准化")


@router.post("/tasks/{task_id}/split", response_model=TaskCreateResponseDto)
async def split_task(task_id: str, body: SplitRequest) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    ok = mgr.split_task(task_id, body.prompt)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 ready_to_split / splitted 可拆分）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="M3 拆分已启动")


@router.post("/tasks/{task_id}/confirm-split", response_model=TaskCreateResponseDto)
async def confirm_split_task(
    task_id: str,
    body: ConfirmSplitRequest = ConfirmSplitRequest(),
) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    ok = mgr.confirm_split(task_id, body.chunks)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 splitted 可确认）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="子文档已确认")


@router.post("/tasks/{task_id}/skip-split", response_model=TaskCreateResponseDto)
async def skip_split_task(task_id: str) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    ok = mgr.skip_split(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 ready_to_split / splitted 可跳过拆分）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="已跳过拆分")


# ---- 本地清洗（v6 新增） -------------------------------------------------


@router.post("/tasks/{task_id}/local-clean", response_model=TaskCreateResponseDto)
async def local_clean_task(
    task_id: str,
    body: LocalCleanRequest = LocalCleanRequest(),
) -> TaskCreateResponseDto:
    """splitted → local_cleaning → local_cleaned。

    异步对每个 chunk 应用 ``apply_local_clean``，覆写 ``split_<N>.md``。
    """
    mgr = _require_task_manager()
    ok = mgr.local_clean_task(task_id, body.options)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 splitted 可本地清洗）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="本地清洗已启动")


@router.post("/tasks/{task_id}/skip-local-clean", response_model=TaskCreateResponseDto)
async def skip_local_clean_task(task_id: str) -> TaskCreateResponseDto:
    """splitted → ready_to_convert，clean_options 留空。"""
    mgr = _require_task_manager()
    ok = mgr.skip_local_clean(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 splitted 可跳过本地清洗）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="已跳过本地清洗")


@router.get("/clean-options", response_model=List[CleanOptionDto])
async def list_clean_options() -> List[CleanOptionDto]:
    """返回本地清洗项元数据（前端复选框渲染用）。"""
    return [
        CleanOptionDto(
            id=opt["id"],  # type: ignore[arg-type]
            label=opt["label"],  # type: ignore[arg-type]
            default=bool(opt.get("default", False)),
            description=opt.get("description"),  # type: ignore[arg-type]
        )
        for opt in _get_clean_options()
    ]


@router.post("/tasks/{task_id}/convert", response_model=TaskCreateResponseDto)
async def convert_task(task_id: str) -> TaskCreateResponseDto:
    mgr = _require_task_manager()
    ok = mgr.convert_task(task_id)
    if not ok:
        record = _task_store.get(task_id) if _task_store else None
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Task status={record.status!r}（仅 ready_to_convert 可启动转换）",
        )
    return TaskCreateResponseDto(task_id=task_id, message="TTS 转换已启动")


@router.get("/split-presets", response_model=List[SplitPresetDto])
async def list_split_presets() -> List[SplitPresetDto]:
    return [
        SplitPresetDto(id=p["id"], name=p["name"], prompt=p["prompt"])
        for p in SPLIT_PRESETS
    ]


@router.get("/normalize-presets", response_model=List[SplitPresetDto])
async def list_normalize_presets() -> List[SplitPresetDto]:
    """v6 起：标准化提示词预设列表（与拆分同形态）。

    ``default`` preset 的 prompt=None，会 fallback 到当前 get_m3_system_prompt() 值。
    """
    out: List[SplitPresetDto] = []
    for p in NORMALIZE_PRESETS:
        prompt_text = p["prompt"] if p["prompt"] is not None else get_m3_system_prompt()
        out.append(SplitPresetDto(id=p["id"], name=p["name"], prompt=prompt_text))
    return out


@router.delete("/tasks/{task_id}", response_model=TaskDeleteResponseDto)
async def delete_task(task_id: str) -> TaskDeleteResponseDto:
    """删除任务：rmtree task_dir + 删 tasks 行。

    前端会弹"确认删除"对话框：done 状态需输入"确认删除"四个字，其他状态二次确认。
    """
    mgr = _require_task_manager()
    if _task_store is None:
        raise HTTPException(status_code=503, detail="TaskStore not initialized")
    # v4：TaskManager 内部已经持有 audio_storage；DELETE 直接走 mgr.delete_task
    result = mgr.delete_task(task_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Task not found.")
    return TaskDeleteResponseDto(**result)


def _to_task_dto(record, *, include_text: bool = False) -> TaskRecordDto:
    """把 TaskRecord 转成 TaskRecordDto；include_text=True 时附带大文本字段（仅 detail API）。"""
    import json as _json
    chunks: Optional[List[str]] = None
    if include_text and record.split_chunks:
        try:
            chunks = _json.loads(record.split_chunks)
        except _json.JSONDecodeError:
            chunks = None
    clean_opts: Optional[List[str]] = None
    if record.clean_options:
        try:
            clean_opts = _json.loads(record.clean_options)
        except _json.JSONDecodeError:
            clean_opts = None
    can_retry = record.status in TASK_RETRYABLE_STATUSES
    return TaskRecordDto(
        task_id=record.task_id,
        filename=record.filename,
        voice_id=record.voice_id,
        status=record.status,
        current_stage=record.current_stage,
        progress=record.progress,
        message=record.message,
        error=record.error,
        date_str=record.date_str,
        created_at=record.created_at,
        updated_at=record.updated_at,
        retry_count=record.retry_count,
        can_retry=can_retry,
        provider=record.provider,
        local_clean_length=len(record.local_clean_text) if record.local_clean_text else None,
        # v6：draft 时返回原文（前端"查看原本"前 300 字预览用）；其他状态为 None
        local_clean_text=record.local_clean_text if record.status == "draft" else None,
        normalized_length=len(record.normalized_text) if record.normalized_text else None,
        normalized_text=record.normalized_text if include_text else None,
        split_prompt=record.split_prompt if include_text else None,
        split_chunks=chunks,
        # v6：splitted / local_cleaned 时返回勾选清洗项（前端面板回显用）
        clean_options=clean_opts if include_text else None,
    )


@router.get("/tasks", response_model=TaskListDto)
async def list_tasks(page: int = 1, size: int = 20) -> TaskListDto:
    store = _require_task_store()
    page = max(1, page)
    size = max(1, min(size, 100))
    items, total = store.list_page(page=page, size=size)
    return TaskListDto(
        items=[_to_task_dto(r, include_text=False) for r in items],
        page=page, size=size, total=total,
    )


@router.get("/tasks/{task_id}", response_model=TaskRecordDto)
async def get_task(task_id: str) -> TaskRecordDto:
    store = _require_task_store()
    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return _to_task_dto(record, include_text=True)


@router.get("/audio/{task_id}")
async def get_audio(task_id: str) -> FileResponse:
    """流式播放 task_dir/<yyyymmdd>/<task_id>/<task_id>.mp3。"""
    audio_svc = _require_audio()
    task_store = _require_task_store()
    record = task_store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    path = audio_svc.resolve(task_id, date_str=record.date_str or None)
    if path is None:
        raise HTTPException(status_code=404, detail="Audio not found.")
    return FileResponse(
        path, media_type="audio/mpeg", filename=f"{task_id}.mp3",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/storage/stats")
async def storage_stats() -> dict:
    return _require_audio().stats()


# ---- Library (听文档) — 直接读 tasks 表 status='done' -------------------


@router.get("/library", response_model=LibraryPageDto)
async def list_library(page: int = 1, size: int = 10) -> LibraryPageDto:
    store = _require_task_store()
    page = max(1, page)
    size = max(1, min(size, 100))
    items, total = store.list_done(page=page, size=size)
    audio_svc = _require_audio()
    library_items = []
    for r in items:
        # 解析 <task_id>.mp3 文件大小 + LRC 是否存在
        mp3_path = audio_svc.task_file_path(
            r.task_id, f"{r.task_id}.mp3", date_str=r.date_str or None,
        )
        byte_size = mp3_path.stat().st_size if mp3_path.exists() else None
        lrc_path = audio_svc.task_file_path(
            r.task_id, f"{r.task_id}.LRC", date_str=r.date_str or None,
        )
        has_lrc = lrc_path.exists()
        library_items.append(LibraryItemDto(
            task_id=r.task_id,
            original_filename=r.filename,
            voice_id=r.voice_id,
            duration_sec=None,
            byte_size=byte_size,
            created_at=r.created_at,
            has_lrc=has_lrc,
            provider=r.provider,
        ))
    return LibraryPageDto(items=library_items, page=page, size=size, total=total)


@router.get("/library/{task_id}", response_model=LibraryDetailDto)
async def get_library_item(task_id: str) -> LibraryDetailDto:
    """听文档详情：从 task_dir 文件系统拼装 original_md + normalized_md。"""
    task_store = _require_task_store()
    audio_svc = _require_audio()
    record = task_store.get(task_id)
    if record is None or record.status != TASK_STATUS_DONE:
        raise HTTPException(status_code=404, detail="Library entry not found.")

    date_str = record.date_str or None
    # original_md ← task_dir/<task_id>.md（本地清洗结果）
    md_path = audio_svc.task_file_path(task_id, f"{task_id}.md", date_str=date_str)
    original_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    # normalized_md ← task_dir/normalization.md
    norm_path = audio_svc.task_file_path(task_id, "normalization.md", date_str=date_str)
    normalized_md = norm_path.read_text(encoding="utf-8") if norm_path.exists() else original_md

    mp3_path = audio_svc.task_file_path(task_id, f"{task_id}.mp3", date_str=date_str)
    byte_size = mp3_path.stat().st_size if mp3_path.exists() else None
    lrc_path = audio_svc.task_file_path(task_id, f"{task_id}.LRC", date_str=date_str)
    lrc_url = f"/api/lyrics/{task_id}.lrc" if lrc_path.exists() else None

    return LibraryDetailDto(
        task_id=record.task_id,
        original_filename=record.filename,
        original_md=original_md,
        normalized_md=normalized_md,
        voice_id=record.voice_id,
        duration_sec=None,
        byte_size=byte_size,
        created_at=record.created_at,
        audio_url=f"/api/audio/{task_id}",
        lrc_url=lrc_url,
        provider=record.provider,
    )


# ---- LRC 字幕文件下载（task_dir/<task_id>.LRC） ---------------------------


@router.get("/lyrics/{filename}")
async def get_lyrics(filename: str) -> FileResponse:
    """下载 task_dir/<task_id>.LRC 文件。filename 形如 ``<task_id>.lrc``。"""
    audio_svc = _require_audio()
    task_store = _require_task_store()
    if "/" in filename or "\\" in filename or not filename.lower().endswith(".lrc"):
        raise HTTPException(status_code=400, detail="Invalid lyrics filename.")
    stem = filename[:-4]  # 去掉 .lrc
    if not stem or not all(c in "0123456789abcdef" for c in stem):
        raise HTTPException(status_code=400, detail="Invalid lyrics filename.")
    record = task_store.get(stem)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    path = audio_svc.resolve_lyrics(stem, date_str=record.date_str or None)
    if path is None:
        raise HTTPException(status_code=404, detail="Lyrics file not found.")
    return FileResponse(
        path, media_type="text/plain; charset=utf-8", filename=filename,
        headers={"Cache-Control": "no-cache"},
    )


# ---- Settings -------------------------------------------------------------


@router.get("/settings", response_model=SettingsDto)
async def get_settings_api(settings: AppSettings = Depends(get_settings)) -> SettingsDto:
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