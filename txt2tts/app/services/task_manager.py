"""后台任务管理器 —— 将 pipeline.run 的 async generator 包装为 asyncio 后台任务。

外部流程：
    1. POST /api/tasks 收到上传 → TaskManager.create_task_and_persist
       - 立即把原始 md 写到 outputs/uploads/<task_id>.md（"先落盘"）
       - 在 tasks 表插入 pending 记录，关联 original_md_path
       - 启动 asyncio.create_task 消费 pipeline.run
    2. pipeline 每产出一个 ProgressEvent，TaskManager 更新 TaskStore。
    3. 异常时（无论 pipeline 主动 error 还是协程异常）：
       - 如果记录了 original_md_path 且文件还在 → status='failed_retryable'，
         保留 md 文件 + 错误信息，前端可点"重试"
       - 否则 status='error'（无法恢复的失败）
    4. POST /api/tasks/{id}/retry → 从磁盘读 md 重新走 pipeline.run，
       递增 retry_count 并清空 error。
    5. 前端通过 GET /api/tasks 和 GET /api/tasks/{id} 轮询进度。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.services.audio_storage import (
    TASK_STATUS_DONE,
    TASK_STATUS_ERROR,
    TASK_STATUS_FAILED_RETRYABLE,
    TASK_STATUS_PENDING,
    TASK_STATUS_PROCESSING,
    AudioStorageService,
    LibraryStore,
    TaskRecord,
    TaskStore,
)
from app.services.pipeline import ProgressEvent, TtsPipeline

logger = logging.getLogger(__name__)


# 任务状态：用于 TaskStore.status 字段（前端识别）
# 状态常量已迁移到 audio_storage.py 以便 watchdog 等其他模块复用；
# 这里 re-export 仅作向后兼容。
__all__ = [
    "TASK_STATUS_PENDING",
    "TASK_STATUS_PROCESSING",
    "TASK_STATUS_DONE",
    "TASK_STATUS_ERROR",
    "TASK_STATUS_FAILED_RETRYABLE",
    "TaskManager",
]


class TaskManager:
    """负责创建 / 运行 / 查询后台 TTS 转换任务（含重试）。"""

    def __init__(
        self,
        pipeline: TtsPipeline,
        task_store: TaskStore,
        *,
        uploads_dir: Path,
        audio_storage: Optional[AudioStorageService] = None,
        library: Optional[LibraryStore] = None,
    ) -> None:
        self._pipeline = pipeline
        self._task_store = task_store
        self._uploads_dir = Path(uploads_dir).resolve()
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        # 可选：注入后 delete_task 能完整清理文件 + 数据库行
        self._audio_storage = audio_storage
        self._library = library

    @property
    def provider(self) -> str:
        """当前 pipeline 使用的 TTS provider（用于 tasks.provider 列）。

        测试里经常传入 ``MagicMock()`` 充当 pipeline，此时 ``_provider``
        也是 MagicMock，需要回落成 ``"mimo"`` 字符串。
        """
        val = getattr(self._pipeline, "_provider", "mimo")
        return val if isinstance(val, str) else "mimo"

    # -- delete -------------------------------------------------------------

    def delete_task(self, task_id: str) -> dict:
        """删除一个任务及其派生文件。

        语义：
            * ``status='done'`` 的任务：保留最终播放所需的 ``audio/<audio_id>.mp3`` +
              ``audio/_artifacts/<audio_id>/``（中间产物快照 + 原始 md），其余全部清空。
              同时从 ``audio_records`` 表删除该条（听文档页不再展示），但 ``audio/<id>.mp3``
              物理文件保留，理论上可被未来的"恢复听文档"或外部引用。
            * 其它状态（pending / processing / error / failed_retryable）：彻底删除
              包括 uploads / chunks / segments / tasks 行。
              如果任务曾经成功过（retry_count>0 但 status 是 failed），也会保留
              旧 audio_id 的成品与 artifacts。

        返回 dict 包含 ``removed`` 文件分类统计、``audio_id``、``status``、``kept_final_audio``。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return {"found": False, "task_id": task_id}

        audio_id = record.audio_id
        # 任务成功过 → 保留最终音频 + artifacts；其它情况一律清空
        keep_final = bool(audio_id) and record.status == TASK_STATUS_DONE

        removed_files: dict = {}
        if self._audio_storage is not None:
            removed_files = self._audio_storage.delete_task_files(
                task_id=task_id,
                audio_id=audio_id,
                keep_final_audio=keep_final,
            )

        # 删 audio_records 行（听文档不再展示）
        # 注意：即使 keep_final=True 也删表行；音频物理文件保留以防后续"复活"
        library_deleted = False
        if self._library is not None and audio_id:
            try:
                library_deleted = self._library.delete(audio_id)
            except Exception:
                logger.exception(
                    "library.delete failed for audio_id=%s", audio_id,
                )

        # 最后删 tasks 行
        tasks_deleted = self._task_store.delete(task_id)

        logger.info(
            "TaskManager.delete_task: task_id=%s status=%s audio_id=%s "
            "keep_final=%s removed=%s library_row=%s",
            task_id, record.status, audio_id, keep_final,
            removed_files, library_deleted,
        )
        return {
            "found": True,
            "task_id": task_id,
            "audio_id": audio_id,
            "status": record.status,
            "kept_final_audio": keep_final,
            "removed_files": removed_files,
            "library_row_deleted": library_deleted,
            "tasks_row_deleted": tasks_deleted,
        }

    # -- public API --------------------------------------------------------

    def create_task(
        self,
        raw: bytes,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
    ) -> str:
        """创建一条任务：

        1. 立即把原始 md 落盘到 uploads/<task_id>.md
        2. 在 tasks 表插入 pending 记录
        3. 启动后台协程消费 pipeline.run

        返回 task_id。
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        task_id = uuid.uuid4().hex
        md_path = self._save_upload(task_id, raw)
        rel_md_path = str(md_path.relative_to(self._uploads_dir.parent)) \
            if md_path.is_relative_to(self._uploads_dir.parent) \
            else str(md_path)

        record = TaskRecord(
            task_id=task_id,
            filename=filename,
            voice_id=voice_id or default_voice_id,
            status=TASK_STATUS_PENDING,
            current_stage=None,
            progress=0.0,
            message="等待处理…",
            audio_id=None,
            error=None,
            created_at=now,
            updated_at=now,
            original_md_path=rel_md_path,
            retry_count=0,
            provider=self.provider,
        )
        self._task_store.insert(record)
        asyncio.create_task(
            self._run_pipeline_from_memory(task_id, raw, filename, voice_id, default_voice_id)
        )
        logger.info("Task %s created for %s (md saved at %s)", task_id, filename, md_path)
        return task_id

    def retry_task(self, task_id: str) -> bool:
        """根据 task_id 重试失败任务。从磁盘读原始 md 重新跑 pipeline。

        返回是否成功触发（False 表示任务不存在 / 状态不允许重试 / md 文件丢失）。
        """
        record = self._task_store.get(task_id)
        if record is None:
            logger.warning("retry_task: task %s not found", task_id)
            return False
        if record.status not in (TASK_STATUS_FAILED_RETRYABLE, TASK_STATUS_ERROR):
            logger.warning("retry_task: task %s status=%s not retryable", task_id, record.status)
            return False
        if not record.original_md_path:
            logger.warning("retry_task: task %s has no original_md_path", task_id)
            return False
        md_path = self._uploads_dir.parent / record.original_md_path
        if not md_path.exists():
            logger.error("retry_task: original md missing at %s", md_path)
            # 标记成不可重试 error
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_ERROR,
                message="原始 md 文件已丢失，无法重试",
                error="original md missing",
            )
            return False
        raw = md_path.read_bytes()
        new_retry_count = record.retry_count + 1
        # 重置 progress / error / status
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_PENDING,
            current_stage=None,
            progress=0.0,
            message=f"准备第 {new_retry_count} 次重试…",
            retry_count=new_retry_count,
            clear_error=True,
        )
        # 启动后台协程
        voice_id = record.voice_id
        asyncio.create_task(
            self._run_pipeline_from_memory(
                task_id, raw, record.filename, voice_id, voice_id,
            )
        )
        logger.info("Task %s retried (retry_count=%d)", task_id, new_retry_count)
        return True

    # -- internal ----------------------------------------------------------

    def _save_upload(self, task_id: str, raw: bytes) -> Path:
        """把原始 md 字节写到 uploads/<task_id>.md，返回绝对路径。"""
        path = self._uploads_dir / f"{task_id}.md"
        path.write_bytes(raw)
        return path

    async def _run_pipeline_from_memory(
        self,
        task_id: str,
        raw: bytes,
        filename: str,
        voice_id: Optional[str],
        default_voice_id: Optional[str],
    ) -> None:
        """从内存中的 md 字节跑 pipeline（适用于首次创建 + 重试）。"""
        # 标记为处理中
        self._task_store.update_progress(
            task_id, status=TASK_STATUS_PROCESSING, current_stage="start",
            message="开始处理…",
        )
        # 把 task_id 暴露给 pipeline，run() 末尾成功后会用 promote_artifacts()
        # 把 chunks/segments/uploads.md 统一搬到 audio/_artifacts/<audio_id>/
        self._pipeline._task_id = task_id
        try:
            async for event in self._pipeline.run(
                raw,
                filename=filename,
                voice_id=voice_id,
                default_voice_id=default_voice_id,
            ):
                self._apply_event(task_id, event)
        except Exception as exc:
            logger.exception("Task %s pipeline crashed", task_id)
            self._mark_failed(task_id, exc)
        finally:
            # 清理挂载（同一 pipeline 实例可能被后续任务复用）
            self._pipeline._task_id = None

    def _mark_failed(self, task_id: str, exc: BaseException) -> None:
        """统一失败处理：检查原始 md 是否仍在，决定 failed_retryable vs error。"""
        record = self._task_store.get(task_id)
        md_alive = (
            record is not None
            and bool(record.original_md_path)
            and (self._uploads_dir.parent / record.original_md_path).exists()
        )
        if md_alive:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_FAILED_RETRYABLE,
                message=f"处理失败（可重试）：{exc}",
                error=str(exc),
            )
            logger.info("Task %s marked failed_retryable (md still on disk)", task_id)
        else:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_ERROR,
                message=f"处理失败（不可重试）：{exc}",
                error=str(exc),
            )
            logger.warning("Task %s marked error (no md for retry)", task_id)

    def _apply_event(self, task_id: str, event: ProgressEvent) -> None:
        """将单个 ProgressEvent 转写为 TaskStore 更新。"""
        if event.stage == "error":
            # pipeline 主动 yield error：与 _mark_failed 同语义
            self._mark_failed(task_id, RuntimeError(event.error or event.message or "error"))
        elif event.stage == "done":
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_DONE,
                current_stage="done",
                progress=1.0,
                message=event.message,
                audio_id=event.audio_id,
                clear_error=True,
            )
        else:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_PROCESSING,
                current_stage=event.stage,
                progress=event.progress,
                message=event.message or "",
            )