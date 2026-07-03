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


# ---- Library (听文档) --------------------------------------------------


class AudioRecordDto(BaseModel):
    """List-view entry: metadata only, no MD content."""

    audio_id: str
    original_filename: str
    voice_id: Optional[str] = None
    duration_sec: Optional[float] = None
    byte_size: int
    created_at: str
    has_lyrics: bool = False
    provider: Optional[str] = None


class LibraryPageDto(BaseModel):
    items: List[AudioRecordDto]
    page: int
    size: int
    total: int


class AudioDetailDto(BaseModel):
    """Detail-view entry: includes original + normalized MD content."""

    audio_id: str
    original_filename: str
    original_md: str
    normalized_md: str
    voice_id: Optional[str] = None
    duration_sec: Optional[float] = None
    byte_size: int
    created_at: str
    audio_url: str
    lyrics_url: Optional[str] = None


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
    audio_id: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    retry_count: int = 0
    can_retry: bool = False  # status == failed_retryable 时为 True
    # 任务使用的 TTS 方案：mimo / edge；前端据此决定详情页展示的步骤条
    provider: Optional[str] = None
    # 分步交互字段（仅 detail API 返回）
    local_clean_length: Optional[int] = None
    normalized_length: Optional[int] = None
    normalized_text: Optional[str] = None
    split_prompt: Optional[str] = None
    split_chunks: Optional[List[str]] = None


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
    audio_id: Optional[str] = None
    status: Optional[str] = None
    kept_final_audio: bool = False
    removed_files: dict = Field(default_factory=dict)
    library_row_deleted: bool = False
    tasks_row_deleted: bool = False


class SplitRequest(BaseModel):
    """拆分文档请求：用户选择的提示词。"""

    prompt: str = Field(description="拆分提示词（内置预设或用户自定义）")


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


# ---- Settings (运行时切换) ---------------------------------------------


class SettingsDto(BaseModel):
    """返回给前端的当前设置。"""

    tts_provider: str = "mimo"
    tts_providers_available: List[str] = ["mimo", "edge"]
    edge_default_voice: str = "zh-CN-XiaoxiaoNeural"
    edge_voices: List[VoiceDto] = []


class SettingsUpdateDto(BaseModel):
    """前端提交的设置更新请求。"""

    tts_provider: Optional[str] = None