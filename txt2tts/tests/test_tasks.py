"""v4 tests for the SQLite-backed TaskStore and TaskManager (无 audio_id)。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.audio_storage import (
    TASK_STATUS_CONVERTING,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_ERROR,
    TASK_STATUS_FAILED_RETRYABLE,
    TASK_STATUS_LOCAL_CLEANED,
    TASK_STATUS_LOCAL_CLEANING,
    TASK_STATUS_NORMALIZED,
    TASK_STATUS_NORMALIZING,
    TASK_STATUS_READY_TO_CONVERT,
    TASK_STATUS_READY_TO_SPLIT,
    TASK_STATUS_SPLITTED,
    TASK_STATUS_SPLITTING,
    TASK_STATUS_SUBTITLE_PENDING,
    AudioStorageService,
    TaskRecord,
    TaskStore,
    task_date_str,
)
from app.services.llm_normalizer import LlmNormalizer
from app.services.pipeline import ProgressEvent, TtsPipeline
from app.services.task_manager import TaskManager


# ---- helpers --------------------------------------------------------------


def _fake_minimax() -> SimpleNamespace:
    return SimpleNamespace(
        _settings=SimpleNamespace(max_input_chars_per_request=10000),
        async_synthesize_segment=AsyncMock(),
    )


def _build_pipeline(audio: AudioStorageService, *, minimax=None,
                     edge=None, provider: str = "minimax",
                     ffmpeg_path: Path = None) -> TtsPipeline:
    """构造一个不依赖 LLM 的 pipeline（llm 仅在被调用时用到）。"""
    llm = MagicMock(spec=LlmNormalizer)  # mock LLM，方法被调时可控
    pipe = TtsPipeline(
        
        llm=llm,
        audio=audio,
        minimax_tts=minimax,
        edge_tts=edge,
        ffmpeg_path=ffmpeg_path,
        provider=provider,
        edge_settings=None,
    )
    return pipe


def _make_manager(tmp_path: Path, *, llm=None, pipeline=None):
    """构造 TaskManager + TaskStore + AudioStorageService + pipeline（v4 接口）。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = pipeline or _build_pipeline(audio)
    mgr = TaskManager(
        pipeline=pipe,
        task_store=task_store,
        audio_storage=audio,
        
        llm=llm if llm is not None else MagicMock(spec=LlmNormalizer),
    )
    return mgr, task_store, audio


# ---- TaskRecord / TaskStore 基础测试 ------------------------------------


def test_task_record_defaults():
    rec = TaskRecord(task_id="t", filename="f", date_str="20260704")
    assert rec.status == "draft"
    assert rec.progress == 0.0
    assert rec.retry_count == 0


def test_task_store_insert_and_get(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    rec = TaskRecord(
        task_id="t1", filename="x.md", status="draft", date_str="20260704",
        created_at="2026-07-04T00:00:00Z", updated_at="2026-07-04T00:00:00Z",
    )
    store.insert(rec)
    got = store.get("t1")
    assert got is not None and got.task_id == "t1" and got.date_str == "20260704"


def test_task_store_get_missing(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    assert store.get("nope") is None


def test_task_store_update_progress(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    store.insert(TaskRecord(task_id="t1", filename="x.md", date_str="20260704",
                              created_at="t", updated_at="t"))
    ok = store.update_progress("t1", status="normalizing", progress=0.5)
    assert ok is True
    assert store.get("t1").status == "normalizing"
    assert store.get("t1").progress == 0.5


def test_task_store_update_progress_nonexistent(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    assert store.update_progress("nope", progress=0.5) is False


def test_task_store_update_partial_keeps_other_fields(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    store.insert(TaskRecord(task_id="t1", filename="x.md", date_str="20260704",
                              status="draft", progress=0.1,
                              created_at="t", updated_at="t",
                              retry_count=3))
    store.update_progress("t1", progress=0.9)
    rec = store.get("t1")
    assert rec.progress == 0.9
    assert rec.retry_count == 3
    assert rec.status == "draft"


def test_task_store_pagination(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    for i in range(5):
        store.insert(TaskRecord(
            task_id=f"t{i}", filename=f"f{i}.md", date_str="20260704",
            status="draft", created_at=f"t{i}", updated_at=f"t{i}",
        ))
    items, total = store.list_page(page=1, size=2)
    assert total == 5 and len(items) == 2
    items2, _ = store.list_page(page=2, size=2)
    assert len(items2) == 2


def test_task_store_insert_get_provider(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    store.insert(TaskRecord(task_id="t1", filename="x.md", date_str="20260704",
                              status="draft", provider="minimax",
                              created_at="t", updated_at="t"))
    assert store.get("t1").provider == "minimax"


def test_task_store_provider_default_none_when_not_set(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    store.insert(TaskRecord(task_id="t1", filename="x.md", date_str="20260704",
                              status="draft", created_at="t", updated_at="t"))
    assert store.get("t1").provider is None


def test_task_store_list_processing(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    store.insert(TaskRecord(task_id="a", filename="x.md", date_str="20260704",
                              status="converting", created_at="t", updated_at="t"))
    store.insert(TaskRecord(task_id="b", filename="x.md", date_str="20260704",
                              status="draft", created_at="t", updated_at="t"))
    rows = store.list_processing()
    assert [r.task_id for r in rows] == ["a"]


# ---- TaskManager 接口测试（v4 接口：audio_storage + task_store + pipeline）


def test_task_manager_provider_default(tmp_path: Path):
    """pipeline._provider='minimax'（默认）→ mgr.provider='minimax'。"""
    mgr, _, _ = _make_manager(tmp_path)
    assert mgr.provider == "minimax"


async def test_task_manager_create_and_run(tmp_path: Path):
    """create_task 写本地清洗 → task_dir/<task_id>.md；DB 记录 local_clean_text + date_str。"""
    mgr, store, audio = _make_manager(tmp_path)
    task_id = mgr.create_task(b"# hello world\n\ncontent", filename="hello.md")
    rec = store.get(task_id)
    assert rec.status == TASK_STATUS_DRAFT
    assert rec.local_clean_text and "hello" in rec.local_clean_text
    # task_dir/<task_id>.md 写盘
    md_path = audio.task_file_path(task_id, f"{task_id}.md", date_str=rec.date_str)
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8") == rec.local_clean_text


async def test_task_manager_pipeline_error_marks_retryable(tmp_path: Path):
    """convert_task 跑 pipeline 抛异常 → _do_convert 捕获后调 _mark_failed → failed_retryable/error。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")

    class BadPipeline:
        _provider = "minimax"
        _task_id = None

        async def run_from_normalized(self, *args, **kwargs):
            raise RuntimeError("pipeline boom")
            if False:  # pragma: no cover
                yield None

    mgr = TaskManager(
        pipeline=BadPipeline(),
        task_store=task_store,
        audio_storage=audio,
        
        llm=MagicMock(spec=LlmNormalizer),
    )
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 模拟 _do_convert：直接跑 pipeline.run_from_normalized
    record = task_store.get(task_id)
    pre_split_chunks = None
    mgr._pipeline._task_id = task_id
    try:
        async for _ in mgr._pipeline.run_from_normalized(
            record.local_clean_text or "",
            filename=record.filename, voice_id=record.voice_id,
            pre_split_chunks=pre_split_chunks,
        ):
            pass
    except Exception as exc:
        mgr._mark_failed(task_id, exc)
    final = task_store.get(task_id)
    # normalized_text 不存在 + md 在 → error（v4：md 在 + normalized 缺 → 不可重试）
    assert final.status == TASK_STATUS_ERROR


def test_task_manager_retry_succeeds_after_failure(tmp_path: Path):
    """retry_task 触发 convert_task。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 把状态改成 failed_retryable
    task_store.update_progress(task_id, status=TASK_STATUS_FAILED_RETRYABLE, error="x",
                                normalized_text="标准化文本", split_chunks=json.dumps(["a"]))
    mgr.convert_task = MagicMock(return_value=True)
    ok = mgr.retry_task(task_id)
    assert ok is True
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    assert rec.retry_count == 1
    mgr.convert_task.assert_called_once_with(task_id)


def test_task_manager_retry_returns_false_when_status_not_failed(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    # status='draft' → retry_task 应该返回 False（不在 TASK_RETRYABLE_STATUSES）
    assert mgr.retry_task(task_id) is False


def test_task_manager_retry_returns_false_when_md_missing(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(task_id, status=TASK_STATUS_FAILED_RETRYABLE, error="x",
                                normalized_text="已标准化")
    # 删 task_dir 模拟 md 丢失
    import shutil
    rec = task_store.get(task_id)
    shutil.rmtree(audio.task_dir(task_id, date_str=rec.date_str))
    assert mgr.retry_task(task_id) is False
    assert task_store.get(task_id).status == TASK_STATUS_ERROR


# ---- 分步流程（v4 接口：mock LLM） --------------------------------------


async def test_create_task_produces_draft_with_local_clean(tmp_path: Path):
    """upload → 本地清洗 → status='draft' + local_clean_text + task_dir/<task_id>.md。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# hello\n\n## section\n\nbody", filename="x.md")
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_DRAFT
    assert rec.local_clean_text and len(rec.local_clean_text) > 0
    md_path = audio.task_file_path(task_id, f"{task_id}.md", date_str=rec.date_str)
    assert md_path.exists()


async def test_normalize_task_transitions_to_ready_to_split(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    llm = MagicMock(spec=LlmNormalizer)
    llm.normalize = AsyncMock(return_value="已标准化文本")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=llm)
    task_id = mgr.create_task(b"# x", filename="x.md")
    mgr.normalize_task(task_id)
    await asyncio.sleep(0.3)
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_SPLIT
    assert rec.normalized_text == "已标准化文本"
    # normalization.md 写盘
    norm_path = audio.task_file_path(task_id, "normalization.md", date_str=rec.date_str)
    assert norm_path.exists()
    assert norm_path.read_text(encoding="utf-8") == "已标准化文本"


async def test_normalize_task_failure_returns_to_draft(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    from app.services.llm_normalizer import LlmNormalizationError
    llm = MagicMock(spec=LlmNormalizer)
    llm.normalize = AsyncMock(side_effect=LlmNormalizationError("m3 down"))
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=llm)
    task_id = mgr.create_task(b"# x", filename="x.md")
    mgr.normalize_task(task_id)
    await asyncio.sleep(0.3)
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_DRAFT
    assert "m3 down" in (rec.error or "")


def test_skip_normalize_uses_local_clean(tmp_path: Path):
    """skip_normalize → 复制 <task_id>.md → normalization.md + status=ready_to_split。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x\n\nbody", filename="x.md")
    ok = mgr.skip_normalize(task_id)
    assert ok is True
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_SPLIT
    norm_path = audio.task_file_path(task_id, "normalization.md", date_str=rec.date_str)
    assert norm_path.exists()
    md_path = audio.task_file_path(task_id, f"{task_id}.md", date_str=rec.date_str)
    assert norm_path.read_text(encoding="utf-8") == md_path.read_text(encoding="utf-8")


async def test_split_task_transitions_to_splitted(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    llm = MagicMock(spec=LlmNormalizer)
    llm.split_text = AsyncMock(return_value=["段落A", "段落B"])
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=llm)
    task_id = mgr.create_task(b"# x", filename="x.md")
    mgr.skip_normalize(task_id)
    mgr.split_task(task_id, prompt="按章节划分")
    await asyncio.sleep(0.3)
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_SPLITTED
    assert json.loads(rec.split_chunks) == ["段落A", "段落B"]
    # split_<N>.md 写盘
    for n in (1, 2):
        p = audio.task_file_path(task_id, f"split_{n}.md", date_str=rec.date_str)
        assert p.exists()


def test_confirm_split_uses_user_chunks_or_original(tmp_path: Path):
    """confirm_split 不传 chunks → 走 split_chunks JSON；传 chunks → 覆盖 split_<N>.md。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    mgr.skip_normalize(task_id)
    # 模拟 splitted 状态 + split_chunks
    task_store.update_progress(
        task_id, status=TASK_STATUS_SPLITTED, split_chunks=json.dumps(["A", "B"], ensure_ascii=False),
    )
    # 不传 chunks → 用 JSON 里的
    ok = mgr.confirm_split(task_id, chunks=None)
    assert ok is True
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    assert json.loads(rec.split_chunks) == ["A", "B"]


def test_confirm_split_filters_empty_chunks(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(
        task_id, status=TASK_STATUS_SPLITTED, split_chunks=json.dumps(["A"], ensure_ascii=False),
    )
    # 全空 → 拒绝
    ok = mgr.confirm_split(task_id, chunks=["", "  "])
    assert ok is False


def test_skip_split_clears_chunks_and_goes_ready_to_convert(tmp_path: Path):
    """skip_split → 复制 normalization.md → split_1.md + status=ready_to_convert。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    mgr.skip_normalize(task_id)
    ok = mgr.skip_split(task_id)
    assert ok is True
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    chunks = json.loads(rec.split_chunks)
    assert len(chunks) == 1
    split_1 = audio.task_file_path(task_id, "split_1.md", date_str=rec.date_str)
    assert split_1.exists()


def test_convert_task_passes_pre_split_chunks(tmp_path: Path):
    """convert_task 把 split_chunks JSON 传给 pipeline.run_from_normalized 的 pre_split_chunks。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")

    captured_kwargs = {}

    class CapturePipeline:
        _provider = "minimax"
        _task_id = None

        async def run_from_normalized(self, normalized, *, filename,
                                       voice_id=None, default_voice_id=None,
                                       pre_split_chunks=None):
            captured_kwargs["normalized"] = normalized
            captured_kwargs["filename"] = filename
            captured_kwargs["voice_id"] = voice_id
            captured_kwargs["pre_split_chunks"] = pre_split_chunks
            # 模拟 pipeline._tts_stage_and_save 的最小 done event
            yield ProgressEvent(stage="tts_synthesize", progress=0.5,
                                  message="synth", provider="minimax")
            yield ProgressEvent(stage="audio_save", progress=0.95,
                                  message="save", provider="minimax")
            yield ProgressEvent(stage="done", progress=1.0, message="ok",
                                  provider="minimax",
                                  subtitle_status="ok", subtitle_error=None)

    mgr = TaskManager(
        pipeline=CapturePipeline(),
        task_store=task_store,
        audio_storage=audio,
        
        llm=MagicMock(spec=LlmNormalizer),
    )
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(
        task_id, status=TASK_STATUS_READY_TO_CONVERT,
        normalized_text="NORMALIZED",
        split_chunks=json.dumps(["chunk1", "chunk2"]),
    )

    # 直接同步驱动 _do_convert，不走 asyncio.create_task
    async def drive():
        mgr._pipeline._task_id = task_id
        try:
            rec = task_store.get(task_id)
            async for event in mgr._pipeline.run_from_normalized(
                rec.normalized_text, filename=rec.filename,
                voice_id=rec.voice_id,
                pre_split_chunks=["chunk1", "chunk2"],
            ):
                mgr._apply_event(task_id, event)
        finally:
            mgr._pipeline._task_id = None

    asyncio.run(drive())
    assert captured_kwargs["pre_split_chunks"] == ["chunk1", "chunk2"]
    assert captured_kwargs["normalized"] == "NORMALIZED"


def test_status_transition_rejects_invalid_state(tmp_path: Path):
    """状态机：normalize_task 仅在 draft 状态可调用。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 设置为 converting → normalize_task 应返回 False
    task_store.update_progress(task_id, status=TASK_STATUS_CONVERTING)
    assert mgr.normalize_task(task_id) is False


# ---- list_done -----------------------------------------------------------


def test_task_store_list_done_only_done(tmp_path: Path):
    store = TaskStore(tmp_path / "lib.db")
    for i, st in enumerate(["done", "draft", "done"]):
        store.insert(TaskRecord(task_id=f"t{i}", filename="f.md", date_str="20260704",
                                  status=st, created_at=f"t{i}", updated_at=f"t{i}"))
    items, total = store.list_done(page=1, size=10)
    assert total == 2
    assert {it.task_id for it in items} == {"t0", "t2"}


# ---- subtitle_pending 状态相关 -------------------------------------------


def test_task_status_subtitle_pending_in_retryable_statuses():
    from app.services.audio_storage import TASK_RETRYABLE_STATUSES
    assert TASK_STATUS_SUBTITLE_PENDING in TASK_RETRYABLE_STATUSES


def test_task_manager_mark_subtitle_pending_sets_status(tmp_path: Path):
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                       audio_storage=audio,
                       
                       llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 直接调内部方法
    mgr._mark_subtitle_pending(task_id, "OSS 404")
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_SUBTITLE_PENDING
    assert "404" in (rec.error or "")


# ---- task_date_str ------------------------------------------------------


def test_task_date_str_format():
    from datetime import datetime
    assert task_date_str(datetime(2026, 7, 4)) == "20260704"
    assert len(task_date_str()) == 8


# ---- v6 本地清洗步骤 ----------------------------------------------------


async def test_local_clean_task_transitions(tmp_path: Path):
    """splitted → local_cleaning → local_cleaned；split_<N>.md 被覆写。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                      audio_storage=audio,
                      
                      llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 模拟 splitted 状态 + 写入 split_<N>.md
    task_store.update_progress(
        task_id,
        status=TASK_STATUS_SPLITTED,
        split_chunks=json.dumps(
            ["详情见 https://example.com 联系 user@test.com",
             "# 标题\n- 列表项"],
            ensure_ascii=False,
        ),
    )
    date_str = task_store.get(task_id).date_str
    audio.task_file_path(task_id, "split_1.md", date_str=date_str).write_text(
        "详情见 https://example.com 联系 user@test.com", encoding="utf-8",
    )
    audio.task_file_path(task_id, "split_2.md", date_str=date_str).write_text(
        "# 标题\n- 列表项", encoding="utf-8",
    )

    ok = mgr.local_clean_task(task_id, ["url", "email", "md_symbols", "list_marks"])
    assert ok is True
    # 立即同步状态（异步协程还在跑）
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_LOCAL_CLEANING
    assert json.loads(rec.clean_options) == ["url", "email", "md_symbols", "list_marks"]

    # 等异步完成
    await asyncio.sleep(0.3)
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_LOCAL_CLEANED
    # 切分内容应已被清洗（无 URL / 邮箱 / # / - 列表）
    chunks = json.loads(rec.split_chunks)
    assert all("http" not in c for c in chunks)
    assert all("@" not in c for c in chunks)
    # split_<N>.md 也已被覆写
    for n, expected_keyword in [(1, "详情见"), (2, "标题")]:
        p = audio.task_file_path(task_id, f"split_{n}.md", date_str=date_str)
        content = p.read_text(encoding="utf-8")
        assert expected_keyword in content
        assert "http" not in content


async def test_local_clean_failure_returns_to_splitted(tmp_path: Path, monkeypatch):
    """_do_local_clean 抛错 → 状态回 splitted，error 写入。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")

    # 让 _do_local_clean 内部的 write_text 抛错 → 触发 except 分支
    from pathlib import Path as _P

    def boom(self, *a, **kw):
        if self.name.startswith("split_"):
            raise OSError("disk full")
        return _P.write_text(self, *a, **kw)

    # 用 monkeypatch 自动恢复（不会泄漏到其他测试）
    monkeypatch.setattr(_P, "write_text", boom)

    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                      audio_storage=audio,

                      llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(
        task_id,
        status=TASK_STATUS_SPLITTED,
        split_chunks=json.dumps(["A 内容"], ensure_ascii=False),
    )
    mgr.local_clean_task(task_id, ["url"])
    await asyncio.sleep(0.3)
    rec = task_store.get(task_id)
    # 失败应回退到 splitted 状态
    assert rec.status == TASK_STATUS_SPLITTED
    assert rec.error and "disk full" in rec.error


def test_skip_local_clean_returns_ready_to_convert(tmp_path: Path):
    """splitted → skip_local_clean → ready_to_convert + clean_options=[]. """
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                      audio_storage=audio,
                      
                      llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(
        task_id, status=TASK_STATUS_SPLITTED,
        split_chunks=json.dumps(["A"], ensure_ascii=False),
    )
    ok = mgr.skip_local_clean(task_id)
    assert ok is True
    rec = task_store.get(task_id)
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    assert rec.clean_options == json.dumps([], ensure_ascii=False)


def test_clean_options_persisted_in_db(tmp_path: Path):
    """clean_options 字段被持久化，_to_task_dto 能读回 list。"""
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = _build_pipeline(audio)
    mgr = TaskManager(pipeline=pipe, task_store=task_store,
                      audio_storage=audio,
                      
                      llm=MagicMock(spec=LlmNormalizer))
    task_id = mgr.create_task(b"# x", filename="x.md")
    task_store.update_progress(
        task_id,
        status=TASK_STATUS_SPLITTED,
        split_chunks=json.dumps(["A"], ensure_ascii=False),
        clean_options=json.dumps(["url", "md_symbols"], ensure_ascii=False),
    )
    rec = task_store.get(task_id)
    assert rec.clean_options is not None
    parsed = json.loads(rec.clean_options)
    assert parsed == ["url", "md_symbols"]