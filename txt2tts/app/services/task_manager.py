"""后台任务管理器 —— 编排分步交互式 TTS 任务（v6：插入本地清洗步骤）。

v5 起移除本地 MarkdownService 清洗（指 M3 之前的预处理），M3 在 normalize
阶段看到原文带 # / * / > / 列表 / 代码块 等结构，自行处理。

v6 起在 ``splitted → ready_to_convert`` 之间插入"本地清洗"步骤：
    - 用户复选清洗项（删除 URL / 邮箱 / 代码片段 / 表情 / Markdown 符号等）
    - apply_local_clean 对每个 chunk 应用规则后**覆写** split_<N>.md
    - 用户也可跳过（直接 splitted → ready_to_convert）

新流程（v4 起 + v5 去本地清洗 + v6 加本地清洗）：
    1. POST /api/tasks 收到上传
       - 解码原始 markdown 文本（utf-8/gbk 兜底）
       - 原文一字不动写入 outputs/<yyyymmdd>/<task_id>/<task_id>.md
       - 在 tasks 表插入 status='draft' 的记录
       - **不**启动任何后台协程；等待用户决定下一步
    2. POST /api/tasks/{id}/normalize    → 异步 M3 标准化（喂原文）
       draft → normalizing → ready_to_split（成功）
                                 → draft（失败，记录 error 允许重试/跳过）
    3. POST /api/tasks/{id}/skip-normalize  → 用原文（<task_id>.md）作为 normalization
    4. POST /api/tasks/{id}/split       → 异步 M3 按用户提示词拆分
       ready_to_split → splitting → splitted（成功）
                                  → ready_to_split（失败）
       → 写 split_<N>.md
    5. POST /api/tasks/{id}/local-clean → 异步本地清洗（v6）
       splitted → local_cleaning → local_cleaned（成功）
                                  → splitted（失败回退）
       → 覆写 split_<N>.md
    6. POST /api/tasks/{id}/skip-local-clean → 跳过清洗（v6）
       splitted → ready_to_convert
    7. POST /api/tasks/{id}/confirm-split → 持久化用户确认的子文档
       splitted/local_cleaned → ready_to_convert
    8. POST /api/tasks/{id}/skip-split  → 复制 normalization.md → split_1.md
    9. POST /api/tasks/{id}/convert     → 异步 TTS 转换（从 TTS 阶段开始）
       ready_to_convert → converting → done（成功）/ error / failed_retryable
                                   → subtitle_pending（minimax 字幕拉取失败，音频仍可用）

阶段感知重试（v4 起 + v6 加 local_cleaning 回退）：
    POST /api/tasks/{id}/retry → 后端根据 status + 已有字段决定从哪一阶段续跑：
        * status=subtitle_pending → 仅重试字幕（走完整 convert）
        * status=local_cleaning  → 回 splitted 后重做 local_clean_task（用历史 clean_options）
        * status=error/failed_retryable：
            - 无 normalized_text  → 重新 normalize
            - 有 normalized_text 无 split_chunks → 重新 split（用历史 prompt）
            - 有 split_chunks  → 重新 convert
        * status=done → 409 不允许
    前端只显示一个统一按钮；subtitle_pending 时按钮文案改为「🔁 重试字幕拉取」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.services.audio_storage import (
    AudioStorageService,
    TASK_STATUS_CONVERTING,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_ERROR,
    TASK_STATUS_FAILED_RETRYABLE,
    TASK_STATUS_LOCAL_CLEANED,
    TASK_STATUS_LOCAL_CLEANING,
    TASK_STATUS_NORMALIZING,
    TASK_STATUS_READY_TO_CONVERT,
    TASK_STATUS_READY_TO_SPLIT,
    TASK_STATUS_SPLITTED,
    TASK_STATUS_SPLITTING,
    TASK_STATUS_SUBTITLE_PENDING,
    TaskRecord,
    TaskStore,
    task_date_str,
)
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.pipeline import ProgressEvent, TtsPipeline
from app.services.text_cleaner import apply_local_clean, clean_summary

logger = logging.getLogger(__name__)


__all__ = [
    "TASK_STATUS_DRAFT",
    "TASK_STATUS_NORMALIZING",
    "TASK_STATUS_NORMALIZED",
    "TASK_STATUS_READY_TO_SPLIT",
    "TASK_STATUS_SPLITTING",
    "TASK_STATUS_SPLITTED",
    "TASK_STATUS_LOCAL_CLEANING",
    "TASK_STATUS_LOCAL_CLEANED",
    "TASK_STATUS_READY_TO_CONVERT",
    "TASK_STATUS_CONVERTING",
    "TASK_STATUS_SUBTITLE_PENDING",
    "TASK_STATUS_DONE",
    "TASK_STATUS_ERROR",
    "TASK_STATUS_FAILED_RETRYABLE",
    "TaskManager",
]


class TaskManager:
    """负责编排分步交互式 TTS 任务的生命周期。

    不再依赖 LibraryStore / uploads_dir / MarkdownService；唯一外部依赖是：
      - audio_storage: 写 task_dir 文件 + 删除整个 task_dir
      - task_store:    SQLite tasks 表读写
      - pipeline:      实际 TTS 转换执行器
      - llm:           分步流程中标准化和拆分需要

    v5 起原始 Markdown 直接由 create_task 写盘 + 入库，M3 在 normalize 阶段处理。
    """

    def __init__(
        self,
        pipeline: TtsPipeline,
        task_store: TaskStore,
        audio_storage: AudioStorageService,
        *,
        llm: Optional[LlmNormalizer] = None,
    ) -> None:
        self._pipeline = pipeline
        self._task_store = task_store
        self._audio = audio_storage
        self._llm = llm

    @property
    def provider(self) -> str:
        """当前 pipeline 使用的 TTS provider。"""
        val = getattr(self._pipeline, "_provider", "minimax")
        return val if isinstance(val, str) else "minimax"

    # -- delete -------------------------------------------------------------

    def delete_task(self, task_id: str) -> dict:
        """删除任务：rmtree 整个 task_dir + 删 tasks 表行。

        听文档数据来自 tasks 表的 status='done' 行；删 task_id 后听文档自动不显示。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return {"found": False, "task_id": task_id}

        # 删整个任务目录（包含 <task_id>.mp3 / SRT / LRC / 中间 md 等所有产物）
        file_result = self._audio.delete_task_files(
            task_id, date_str=record.date_str or None,
        )

        # 删 tasks 表行
        deleted = self._task_store.delete(task_id)

        logger.info(
            "TaskManager.delete_task: task_id=%s status=%s date=%s "
            "removed_files=%s tasks_row=%s",
            task_id, record.status, record.date_str,
            file_result, deleted,
        )
        return {
            "found": True,
            "task_id": task_id,
            "status": record.status,
            "removed_files": file_result,
            "tasks_row_deleted": deleted,
        }

    # -- create (upload) ---------------------------------------------------

    def create_task(
        self,
        raw: bytes,
        *,
        filename: str,
        voice_id: Optional[str] = None,
        default_voice_id: Optional[str] = None,
    ) -> str:
        """创建一条草稿任务。

        v5 起：原始 Markdown 直接落盘 + 入库，不做本地清洗。
        1. 解码原始 markdown 文本（utf-8/gbk 兜底）
        2. 原文一字不动写到 outputs/<yyyymmdd>/<task_id>/<task_id>.md
        3. 在 tasks 表插入 status='draft' 的记录，local_clean_text 存原文
           （v4 旧字段保留，含义从『本地清洗结果』改为『原文』，跳过标准化时复用）

        返回 task_id。
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        task_id = uuid.uuid4().hex
        date_str = task_date_str()

        # v5：原始 markdown 文本（不做任何清洗）
        try:
            raw_text = self._decode(raw)
        except Exception as exc:
            logger.exception("decode raw bytes failed for %s", filename)
            raw_text = ""
            decode_error = str(exc)
        else:
            decode_error = None

        task_dir = self._audio.task_dir(task_id, date_str=date_str)
        task_md_path = task_dir / f"{task_id}.md"
        try:
            task_md_path.write_text(raw_text, encoding="utf-8")
        except Exception as exc:
            logger.exception("write task_dir/<task_id>.md failed")
            decode_error = f"{decode_error or ''}; write failed: {exc}"

        original_md_path = f"{date_str}/{task_id}/{task_id}.md"
        message = (
            f"原文已落盘 · {len(raw_text)} 字符 · 待标准化"
            if raw_text else f"原文写入失败：{decode_error}"
        )
        record = TaskRecord(
            task_id=task_id,
            filename=filename,
            voice_id=voice_id or default_voice_id,
            status=TASK_STATUS_DRAFT,
            current_stage="draft",
            progress=0.0,
            message=message,
            date_str=date_str,
            error=decode_error,
            created_at=now,
            updated_at=now,
            retry_count=0,
            provider=self.provider,
            original_md_path=original_md_path,
            local_clean_text=raw_text,
        )
        self._task_store.insert(record)
        logger.info(
            "Task %s created as draft (date=%s, raw_text=%d chars, path=%s)",
            task_id, date_str, len(raw_text), task_md_path,
        )
        return task_id

    # -- normalize step ----------------------------------------------------

    def normalize_task(self, task_id: str, system_prompt: Optional[str] = None) -> bool:
        """触发 M3 标准化（异步）。仅 draft 状态可调用。

        v6 起支持自定义 system_prompt：None → llm_normalizer 内部走
        get_m3_system_prompt() 默认值；非 None → 直接传给 M3。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status != TASK_STATUS_DRAFT:
            logger.warning(
                "normalize_task: task %s status=%s (not draft)", task_id, record.status,
            )
            return False
        if self._llm is None:
            raise RuntimeError("normalize_task requires llm service injection")

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_NORMALIZING,
            current_stage="llm_normalize",
            progress=0.15,
            message="M3 标准化中…",
            clear_error=True,
        )
        asyncio.create_task(self._do_normalize(task_id, record, system_prompt))
        return True

    async def _do_normalize(
        self, task_id: str, record: TaskRecord,
        system_prompt: Optional[str] = None,
    ) -> None:
        """后台协程：M3 标准化 draft 任务，写 normalization.md 到 task_dir。"""
        try:
            normalized = await self._llm.normalize(
                record.local_clean_text or "",
                system=system_prompt,
            )
        except LlmNormalizationError as exc:
            logger.exception("normalize_task %s failed", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_DRAFT,
                current_stage="llm_normalize",
                message=f"M3 标准化失败：{exc}",
                error=str(exc),
            )
            return
        except Exception as exc:
            logger.exception("normalize_task %s crashed", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_DRAFT,
                current_stage="llm_normalize",
                message=f"M3 标准化异常：{exc}",
                error=str(exc),
            )
            return

        # 写 normalization.md 到 task_dir
        try:
            task_md = self._audio.task_file_path(
                task_id, "normalization.md", date_str=record.date_str,
            )
            task_md.write_text(normalized, encoding="utf-8")
        except Exception:
            logger.exception("write normalization.md failed for %s", task_id)

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_SPLIT,
            current_stage="llm_normalize",
            progress=0.30,
            message=f"M3 标准化完成 · {len(normalized)} 字符 · 待拆分",
            normalized_text=normalized,
            clear_error=True,
        )
        logger.info("Task %s normalized (%d chars)", task_id, len(normalized))

    def skip_normalize(self, task_id: str) -> bool:
        """跳过标准化：复制 ``<task_id>.md`` 到 ``normalization.md``。"""
        record = self._task_store.get(task_id)
        if record is None or record.status != TASK_STATUS_DRAFT:
            return False
        date_str = record.date_str
        src = self._audio.task_file_path(task_id, f"{task_id}.md", date_str=date_str)
        dst = self._audio.task_file_path(task_id, "normalization.md", date_str=date_str)
        try:
            if src.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            logger.exception("skip_normalize copy failed for %s", task_id)
            return False
        normalized = record.local_clean_text or ""
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_SPLIT,
            current_stage="draft",
            progress=0.20,
            message="已跳过标准化（原文直接进拆分）· 待拆分",
            normalized_text=normalized,
        )
        return True

    # -- split step --------------------------------------------------------

    def split_task(self, task_id: str, prompt: str) -> bool:
        """触发 M3 拆分（异步）。仅 ready_to_split / splitted 可调用（允许重新拆分）。"""
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status not in (TASK_STATUS_READY_TO_SPLIT, TASK_STATUS_SPLITTED):
            logger.warning(
                "split_task: task %s status=%s (not ready_to_split/splitted)",
                task_id, record.status,
            )
            return False
        if not record.normalized_text:
            return False
        if self._llm is None:
            raise RuntimeError("split_task requires llm service injection")

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_SPLITTING,
            current_stage="llm_split",
            progress=0.45,
            message="M3 拆分子文档中…",
            split_prompt=prompt,
            clear_error=True,
        )
        asyncio.create_task(self._do_split(task_id, record, prompt))
        return True

    async def _do_split(self, task_id: str, record: TaskRecord, prompt: str) -> None:
        """后台协程：M3 拆分 normalized_text，写 split_<N>.md 到 task_dir。"""
        try:
            chunks = await self._llm.split_text(
                record.normalized_text,  # type: ignore[arg-type]
                system=prompt,
            )
        except LlmNormalizationError as exc:
            logger.exception("split_task %s failed", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_READY_TO_SPLIT,
                current_stage="llm_split",
                message=f"M3 拆分失败：{exc}",
                error=str(exc),
            )
            return
        except Exception as exc:
            logger.exception("split_task %s crashed", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_READY_TO_SPLIT,
                current_stage="llm_split",
                message=f"M3 拆分异常：{exc}",
                error=str(exc),
            )
            return

        # 写 split_<N>.md
        try:
            for i, ch in enumerate(chunks, 1):
                p = self._audio.task_file_path(
                    task_id, f"split_{i}.md", date_str=record.date_str,
                )
                p.write_text(ch, encoding="utf-8")
        except Exception:
            logger.exception("write split_<N>.md failed for %s", task_id)

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_SPLITTED,
            current_stage="llm_split",
            progress=0.55,
            message=f"M3 拆分完成 · {len(chunks)} 个子文档 · 待确认",
            split_chunks=json.dumps(chunks, ensure_ascii=False),
            clear_error=True,
        )
        logger.info("Task %s split into %d chunks", task_id, len(chunks))

    def confirm_split(
        self,
        task_id: str,
        chunks: Optional[List[str]] = None,
    ) -> bool:
        """用户确认子文档 → ready_to_convert。

        chunks=None：使用 M3 原始拆分结果（从 split_<N>.md 重读，或 split_chunks JSON）。
        chunks=list：使用用户编辑后的子文档（同时覆盖写 split_<N>.md）。

        v6 起：从 ``splitted`` 或 ``local_cleaned`` 都可确认。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status not in (TASK_STATUS_SPLITTED, TASK_STATUS_LOCAL_CLEANED):
            return False
        date_str = record.date_str

        if chunks is None:
            # 默认走数据库里 M3 原始拆分结果；同时校验 split_<N>.md 是否都存在
            if not record.split_chunks:
                return False
            try:
                chunks = json.loads(record.split_chunks)
            except json.JSONDecodeError:
                logger.exception("split_chunks 解析失败 for task %s", task_id)
                return False
            # 校验 split_<N>.md 都存在
            for i in range(1, len(chunks) + 1):
                p = self._audio.task_file_path(task_id, f"split_{i}.md", date_str=date_str)
                if not p.exists():
                    logger.warning("confirm_split: split_%d.md missing for %s", i, task_id)
        # 过滤空 chunk
        chunks = [c for c in chunks if c and c.strip()]
        if not chunks:
            self._task_store.update_progress(
                task_id,
                error="子文档全部为空，请重新拆分或跳过",
            )
            return False
        # 同步重写 split_<N>.md（用户编辑后覆盖）
        try:
            for i, ch in enumerate(chunks, 1):
                p = self._audio.task_file_path(
                    task_id, f"split_{i}.md", date_str=date_str,
                )
                p.write_text(ch, encoding="utf-8")
        except Exception:
            logger.exception("confirm_split write split_<N>.md failed for %s", task_id)

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_CONVERT,
            current_stage="ready",
            progress=0.65,
            message=f"子文档已确认 · {len(chunks)} 个 · 待转换",
            split_chunks=json.dumps(chunks, ensure_ascii=False),
        )
        return True

    def skip_split(self, task_id: str) -> bool:
        """跳过拆分：复制 normalization.md → split_1.md。"""
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status not in (TASK_STATUS_READY_TO_SPLIT, TASK_STATUS_SPLITTED):
            return False
        date_str = record.date_str
        src = self._audio.task_file_path(task_id, "normalization.md", date_str=date_str)
        dst = self._audio.task_file_path(task_id, "split_1.md", date_str=date_str)
        try:
            if src.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            logger.exception("skip_split copy failed for %s", task_id)
            return False
        chunks = [src.read_text(encoding="utf-8")]
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_CONVERT,
            current_stage="ready",
            progress=0.60,
            message="已跳过拆分 · 待转换（将使用自动切分）",
            split_chunks=json.dumps(chunks, ensure_ascii=False),
        )
        return True

    # -- local clean step (v6) ---------------------------------------------

    def local_clean_task(self, task_id: str, enabled_ids: List[str]) -> bool:
        """splitted → local_cleaning → local_cleaned

        异步对每个 chunk 应用 ``apply_local_clean``，覆写 ``split_<N>.md``。
        失败回 ``splitted``；前端可继续重新发起。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status != TASK_STATUS_SPLITTED:
            logger.warning(
                "local_clean_task: task %s status=%s (not splitted)",
                task_id, record.status,
            )
            return False
        if not record.split_chunks:
            self._task_store.update_progress(
                task_id,
                error="缺少 split_chunks，无法本地清洗",
            )
            return False
        # 校验 enabled_ids：未知 id 静默丢弃（保持与 apply_local_clean 一致）
        from app.services.text_cleaner import CLEAN_OPTIONS as _CO
        valid_ids = {opt["id"] for opt in _CO}
        enabled_ids = [cid for cid in enabled_ids if cid in valid_ids]

        # 立即切到 local_cleaning，写入 clean_options
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_LOCAL_CLEANING,
            current_stage="local_clean",
            progress=0.62,
            message=f"本地清洗中…（{len(enabled_ids)} 项）",
            clean_options=json.dumps(enabled_ids, ensure_ascii=False),
            clear_error=True,
        )
        # 解析 chunks
        try:
            chunks = json.loads(record.split_chunks)
        except json.JSONDecodeError:
            logger.exception("local_clean_task split_chunks parse failed for %s", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_SPLITTED,
                error="split_chunks 解析失败",
            )
            return False

        asyncio.create_task(self._do_local_clean(task_id, chunks, enabled_ids))
        return True

    async def _do_local_clean(
        self, task_id: str, chunks: List[str], enabled_ids: List[str],
    ) -> None:
        """后台协程：对每个 chunk 应用 apply_local_clean，覆写 split_<N>.md。

        本地清洗是纯正则替换，无网络/IO 调用；理论上不应失败，但仍保留
        异常回退（disk full / permission error 等）。
        """
        record = self._task_store.get(task_id)
        if record is None:
            return
        date_str = record.date_str
        try:
            cleaned = [
                apply_local_clean(c, enabled_ids) if c and c.strip() else c
                for c in chunks
            ]
            for i, c in enumerate(cleaned, 1):
                p = self._audio.task_file_path(
                    task_id, f"split_{i}.md", date_str=date_str,
                )
                p.write_text(c, encoding="utf-8")
            before_total = sum(len(c) for c in chunks if c)
            after_total = sum(len(c) for c in cleaned if c)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_LOCAL_CLEANED,
                current_stage="local_cleaned",
                progress=0.65,
                message=(
                    f"本地清洗完成 · {len(enabled_ids)} 项 · "
                    f"{len(cleaned)} 个子文档 · "
                    f"删除 {max(0, before_total - after_total)} 字符"
                ),
                split_chunks=json.dumps(cleaned, ensure_ascii=False),
            )
            logger.info(
                "local_clean_task %s done: %d options, %d chunks, %d → %d chars",
                task_id, len(enabled_ids), len(cleaned), before_total, after_total,
            )
        except Exception as exc:
            logger.exception("local_clean_task %s failed", task_id)
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_SPLITTED,
                error=f"local_clean failed: {exc}",
            )

    def skip_local_clean(self, task_id: str) -> bool:
        """跳过本地清洗：splitted → ready_to_convert，clean_options 留空。"""
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status != TASK_STATUS_SPLITTED:
            logger.warning(
                "skip_local_clean: task %s status=%s (not splitted)",
                task_id, record.status,
            )
            return False
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_CONVERT,
            current_stage="ready",
            progress=0.65,
            message="已跳过本地清洗 · 待转换",
            clean_options=json.dumps([], ensure_ascii=False),
        )
        return True

    # -- convert step ------------------------------------------------------

    def convert_task(self, task_id: str) -> bool:
        """启动 TTS 转换（异步）。仅 ready_to_convert 状态可调用。"""
        record = self._task_store.get(task_id)
        if record is None:
            return False
        if record.status != TASK_STATUS_READY_TO_CONVERT:
            logger.warning(
                "convert_task: task %s status=%s (not ready_to_convert)",
                task_id, record.status,
            )
            return False
        if not record.normalized_text:
            return False

        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_CONVERTING,
            current_stage="tts_synthesize",
            progress=0.70,
            message="TTS 转换中…",
            clear_error=True,
        )
        asyncio.create_task(self._do_convert(task_id, record))
        return True

    async def _do_convert(self, task_id: str, record: TaskRecord) -> None:
        """后台协程：消费 pipeline.run_from_normalized。

        minimax 字幕失败处理：done event 携带 subtitle_status='pending' 时
        任务转 subtitle_pending；pipeline 已写好 <task_id>.mp3，音频可听。
        """
        pre_split_chunks: Optional[List[str]] = None
        if record.split_chunks:
            try:
                pre_split_chunks = json.loads(record.split_chunks)
                if not pre_split_chunks:
                    pre_split_chunks = None
            except json.JSONDecodeError:
                pre_split_chunks = None

        # 把 task_id 暴露给 pipeline（用于写 task_dir/.../<task_id>.{md,mp3,srt,lrc}）
        self._pipeline._task_id = task_id
        try:
            async for event in self._pipeline.run_from_normalized(
                record.normalized_text,  # type: ignore[arg-type]
                filename=record.filename,
                voice_id=record.voice_id,
                pre_split_chunks=pre_split_chunks,
            ):
                self._apply_event(task_id, event)
                if event.stage == "done" and event.subtitle_status == "pending":
                    self._mark_subtitle_pending(task_id, event.subtitle_error or "")
        except Exception as exc:
            logger.exception("convert_task %s pipeline crashed", task_id)
            self._mark_failed(task_id, exc)
        finally:
            self._pipeline._task_id = None

    # -- retry (v4 阶段感知) -----------------------------------------------

    def retry_task(self, task_id: str) -> bool:
        """阶段感知重试（详见模块 docstring 决策表）。"""
        record = self._task_store.get(task_id)
        if record is None:
            return False

        new_retry_count = record.retry_count + 1

        # 1) 字幕待重试
        if record.status == TASK_STATUS_SUBTITLE_PENDING:
            return self._retry_subtitle(task_id, record, new_retry_count)

        # 2) error / failed_retryable
        if record.status in (TASK_STATUS_FAILED_RETRYABLE, TASK_STATUS_ERROR):
            # 检查原始 md 完整性（任何阶段都需要）
            if not record.original_md_path:
                self._task_store.update_progress(
                    task_id,
                    status=TASK_STATUS_ERROR,
                    message="任务记录缺少 original_md_path，无法重试",
                    error="original_md_path missing",
                )
                return False
            md_full = self._audio._root / record.original_md_path
            if not md_full.exists():
                self._task_store.update_progress(
                    task_id,
                    status=TASK_STATUS_ERROR,
                    message="原始 md 文件已丢失，无法重试",
                    error="original md missing",
                )
                return False

            # 没有 normalized_text → 从 normalize 开始
            if not record.normalized_text:
                if self._llm is None:
                    self._task_store.update_progress(
                        task_id,
                        status=TASK_STATUS_ERROR,
                        message="LLM 服务未配置，无法重试 normalize",
                        error="llm not configured",
                    )
                    return False
                self._task_store.update_progress(
                    task_id,
                    status=TASK_STATUS_DRAFT,
                    current_stage="draft",
                    progress=0.05,
                    message=f"准备第 {new_retry_count} 次重试（重做标准化）…",
                    retry_count=new_retry_count,
                    clear_error=True,
                )
                logger.info(
                    "retry_task: %s 从 normalize 阶段续跑（retry #%d）",
                    task_id, new_retry_count,
                )
                return self.normalize_task(task_id)

            # 有 normalized_text 但没 split_chunks → 从 split 开始
            if not record.split_chunks:
                if self._llm is None:
                    return self._retry_to_convert(task_id, record, new_retry_count,
                                                   note="无 LLM，跳过拆分直接重转")
                prompt = record.split_prompt or ""
                self._task_store.update_progress(
                    task_id,
                    status=TASK_STATUS_READY_TO_SPLIT,
                    current_stage="ready",
                    progress=0.40,
                    message=f"准备第 {new_retry_count} 次重试（重做拆分）…",
                    retry_count=new_retry_count,
                    clear_error=True,
                )
                logger.info(
                    "retry_task: %s 从 split 阶段续跑（retry #%d, prompt=%d chars）",
                    task_id, new_retry_count, len(prompt),
                )
                return self.split_task(task_id, prompt)

            # 局部清洗中途失败（罕见：纯正则，但保留防御）→ 回 splitted
            if record.status == TASK_STATUS_LOCAL_CLEANING:
                self._task_store.update_progress(
                    task_id,
                    status=TASK_STATUS_SPLITTED,
                    current_stage="splitted",
                    progress=0.60,
                    message=f"准备第 {new_retry_count} 次重试（重新本地清洗）…",
                    retry_count=new_retry_count,
                    clear_error=True,
                )
                enabled = []
                if record.clean_options:
                    try:
                        enabled = json.loads(record.clean_options)
                    except json.JSONDecodeError:
                        enabled = []
                logger.info(
                    "retry_task: %s 从 local_clean 阶段续跑（retry #%d, %d options）",
                    task_id, new_retry_count, len(enabled),
                )
                return self.local_clean_task(task_id, enabled)

            # 都有：直接重跑 convert
            return self._retry_to_convert(task_id, record, new_retry_count,
                                           note="从 convert 阶段续跑")

        # 3) 其他状态不允许
        return False

    def _retry_to_convert(
        self, task_id: str, record: TaskRecord, new_retry_count: int, *,
        note: str,
    ) -> bool:
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_CONVERT,
            current_stage="ready",
            progress=0.60,
            message=f"准备第 {new_retry_count} 次重试（{note}）…",
            retry_count=new_retry_count,
            clear_error=True,
        )
        logger.info(
            "retry_task: %s %s（retry #%d）", task_id, note, new_retry_count,
        )
        return self.convert_task(task_id)

    def _retry_subtitle(
        self, task_id: str, record: TaskRecord, new_retry_count: int,
    ) -> bool:
        """仅重试字幕拉取：走完整 convert（minimax 当前字幕与音频合成耦合）。"""
        if not record.normalized_text:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_FAILED_RETRYABLE,
                message="重试字幕失败：缺少 normalized_text",
                error="retry_subtitle: missing normalized_text",
            )
            return False
        self._task_store.update_progress(
            task_id,
            status=TASK_STATUS_READY_TO_CONVERT,
            current_stage="subtitle_retry",
            progress=0.60,
            message=f"准备第 {new_retry_count} 次字幕重试（重新跑 convert）…",
            retry_count=new_retry_count,
            clear_error=True,
        )
        logger.info(
            "retry_task: %s 仅字幕重试（retry #%d），走完整 convert",
            task_id, new_retry_count,
        )
        return self.convert_task(task_id)

    # -- internal ----------------------------------------------------------

    def _mark_failed(self, task_id: str, exc: BaseException) -> None:
        """统一失败处理：检查原始 md 是否仍在，决定 failed_retryable vs error。"""
        record = self._task_store.get(task_id)
        md_alive = (
            record is not None
            and bool(record.original_md_path)
            and (self._audio._root / record.original_md_path).exists()
        )
        if md_alive and record and record.normalized_text:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_FAILED_RETRYABLE,
                message=f"处理失败（可重试）：{exc}",
                error=str(exc),
            )
        else:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_ERROR,
                message=f"处理失败（不可重试）：{exc}",
                error=str(exc),
            )

    def _mark_subtitle_pending(self, task_id: str, subtitle_error: str) -> None:
        """把任务置为 subtitle_pending。

        音频 bytes 已由 pipeline 内部写到 task_dir/<task_id>.mp3，
        这里只需把 TaskStore.status 切到 subtitle_pending。
        """
        record = self._task_store.get(task_id)
        date_str = record.date_str if record else None
        self._task_store.mark_subtitle_pending(
            task_id, date_str=date_str or "", subtitle_error=subtitle_error,
        )
        logger.warning(
            "convert_task %s 字幕拉取失败，转 subtitle_pending: %s",
            task_id, subtitle_error,
        )

    def _apply_event(self, task_id: str, event: ProgressEvent) -> None:
        """将单个 ProgressEvent 转写为 TaskStore 更新。"""
        if event.stage == "error":
            self._mark_failed(task_id, RuntimeError(event.error or event.message or "error"))
        elif event.stage == "done":
            # 字幕待重试：跳过（外层 _do_convert 已调 _mark_subtitle_pending）
            if event.subtitle_status == "pending":
                return
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_DONE,
                current_stage="done",
                progress=1.0,
                message=event.message,
                clear_error=True,
            )
        else:
            self._task_store.update_progress(
                task_id,
                status=TASK_STATUS_CONVERTING,
                current_stage=event.stage,
                progress=event.progress,
                message=event.message or "",
            )

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="ignore")