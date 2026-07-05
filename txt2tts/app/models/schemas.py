"""Pydantic request/response schemas (v4: task_id 一统天下，无 audio_id)。"""
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


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


# ---- Library (听文档) — 直接从 tasks 表 status='done' 取 ----------------


class LibraryItemDto(BaseModel):
    """List-view entry: metadata only.

    听文档列表（status='done' 的任务）。
    """

    task_id: str                                # 也是最终播放文件名
    original_filename: str
    voice_id: Optional[str] = None
    duration_sec: Optional[float] = None
    byte_size: Optional[int] = None
    created_at: str
    has_lrc: bool = False                       # task_dir/<task_id>.LRC 是否存在
    provider: Optional[str] = None              # "minimax" | "edge"


class LibraryPageDto(BaseModel):
    items: List[LibraryItemDto]
    page: int
    size: int
    total: int


class LibraryDetailDto(BaseModel):
    """Detail-view entry: 含原文 + 标准化 MD 内容。

    字段从 task_record + task_dir 文件系统拼装：
        - original_md   ← task_dir/<task_id>.md
        - normalized_md ← task_dir/normalization.md
    """

    task_id: str
    original_filename: str
    original_md: str
    normalized_md: str
    voice_id: Optional[str] = None
    duration_sec: Optional[float] = None
    byte_size: Optional[int] = None
    created_at: str
    audio_url: str                              # /api/audio/{task_id}
    lrc_url: Optional[str] = None              # /api/lyrics/{task_id}.lrc
    provider: Optional[str] = None


# ---- Tasks (上传转语音) -------------------------------------------------


class TaskRecordDto(BaseModel):
    """列表视图：任务元信息。"""

    task_id: str
    filename: str
    voice_id: Optional[str] = None
    status: str
    current_stage: Optional[str] = None
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    date_str: str = ""                                # "yyyyMMdd"
    created_at: str
    updated_at: str
    retry_count: int = 0
    can_retry: bool = False  # status ∈ TASK_RETRYABLE_STATUSES 时为 True
    provider: Optional[str] = None
    # 分步交互字段（仅 detail API 返回）
    local_clean_length: Optional[int] = None
    # v6 起：draft 时返回**原文**（其他状态为 None）—— 给前端"查看原本"展示用
    local_clean_text: Optional[str] = None
    normalized_length: Optional[int] = None
    normalized_text: Optional[str] = None
    split_prompt: Optional[str] = None
    split_chunks: Optional[List[str]] = None
    # v6 起：splitted / local_cleaned 时返回**用户勾选**的清洗项 id 列表
    clean_options: Optional[List[str]] = None


class TaskListDto(BaseModel):
    items: List[TaskRecordDto]
    page: int
    size: int
    total: int


class TaskCreateResponseDto(BaseModel):
    """上传成功后返回 task_id 供前端轮询。"""

    task_id: str
    message: str = "任务已创建"


class TaskDeleteResponseDto(BaseModel):
    """DELETE /api/tasks/{id} 返回值。"""

    found: bool
    task_id: str
    status: Optional[str] = None
    removed_files: dict = Field(default_factory=dict)
    tasks_row_deleted: bool = False


class SplitRequest(BaseModel):
    """拆分文档请求：用户选择的提示词。"""

    prompt: str = Field(description="拆分提示词（内置预设或用户自定义）")


class NormalizeRequest(BaseModel):
    """标准化请求：可选自定义系统 prompt。留空则用 get_m3_system_prompt() 默认值。"""

    prompt: Optional[str] = Field(
        default=None,
        description="M3 标准化 system prompt（留空 → 默认）",
    )


class ConfirmSplitRequest(BaseModel):
    """确认拆分请求：用户可能编辑过的子文档列表。"""

    chunks: Optional[List[str]] = Field(
        default=None,
        description="确认后的子文档列表。None = 使用 M3 原始拆分结果",
    )


class SplitPresetDto(BaseModel):
    """内置拆分提示词。"""

    id: str
    name: str
    prompt: str


# ---- 本地清洗（v6） -------------------------------------------------------


class CleanOptionDto(BaseModel):
    """清洗项元数据：前端 GET /api/clean-options 拿到后渲染复选框。"""

    id: str
    label: str
    default: bool = False
    description: Optional[str] = None


class LocalCleanRequest(BaseModel):
    """本地清洗请求：用户勾选的清洗项 id 列表。

    空列表等价于 skip（前端可直接调 skip 端点更明确）。
    """

    options: List[str] = Field(
        default_factory=list,
        description="启用的清洗项 id 列表（来自 GET /api/clean-options）",
    )


# ---- Settings (运行时切换) ---------------------------------------------


class SettingsDto(BaseModel):
    """返回给前端的当前设置。"""

    tts_provider: str = "minimax"
    tts_providers_available: List[str] = ["minimax", "edge"]
    edge_default_voice: str = "zh-CN-XiaoxiaoNeural"
    edge_voices: List[VoiceDto] = []


class SettingsUpdateDto(BaseModel):
    """前端提交的设置更新请求。"""

    tts_provider: Optional[str] = None