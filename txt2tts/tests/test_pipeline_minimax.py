"""v5 tests for minimax provider pipeline + subtitle_pending 状态机。

v5 改造点：
    * AudioStorageService 已大瘦身（无 LibraryStore / 无 promote_artifacts）
    * 全部产物在 task_dir/<task_id>/ 下
    * TaskManager 用 TaskStore + audio_storage 两个依赖
    * **去除 MarkdownService**：v5 起原始 MD 直接送 M3，无本地清洗阶段
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import MinimaxTtsSettings
from app.services.audio_storage import (
    TASK_STATUS_CONVERTING,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_ERROR,
    TASK_STATUS_FAILED_RETRYABLE,
    TASK_STATUS_READY_TO_CONVERT,
    TASK_STATUS_READY_TO_SPLIT,
    TASK_STATUS_SUBTITLE_PENDING,
    AudioStorageService,
    TaskRecord,
    TaskStore,
    task_date_str,
)
from app.services.minimax_tts_provider import ProviderResult
from app.services.pipeline import TtsPipeline
from app.services.task_manager import TaskManager


# ---- helpers --------------------------------------------------------------


def _mp3_bytes(seed: int = 0) -> bytes:
    return b"\x49\x44\x33" + bytes([seed]) * 100 + b"\xff\xfb\x90\x00" + b"\x00" * 50


class FakeMinimax:
    def __init__(self, *, audio: bytes = None, subtitle_error: str = None,
                 cues: list = None, duration_sec: float = 1.0):
        self._audio = audio if audio is not None else _mp3_bytes(0)
        self._subtitle_error = subtitle_error
        self._cues = cues if cues is not None else [(0.0, 0.5, "第一句"), (0.5, 1.0, "第二句")]
        self._duration_sec = duration_sec
        self._settings = SimpleNamespace(max_input_chars_per_request=10000)

    async def synthesize_segment(self, text, *, voice=None, title="", artist="txt2tts"):
        return ProviderResult(
            audio_bytes=self._audio,
            duration_sec=self._duration_sec,
            srt_text="",
            lrc_text="",
            sentence_cues=self._cues,
            subtitle_fetch_error=self._subtitle_error,
        )


class FakeLlm:
    def __init__(self, split_chunks: list = None):
        self._split_chunks = split_chunks or ["chunk1", "chunk2"]

    async def normalize(self, t):
        return "NORMALIZED:" + t

    async def split_text(self, text, *, max_chars=6000, system=None):
        return self._split_chunks


def _pipeline_with_audio(audio_svc, minimax=None, llm=None):
    """构造 TtsPipeline + mock ffmpeg_concat（不依赖真 ffmpeg）。"""
    bin_ffmpeg = Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe"
    p = TtsPipeline(
        llm=llm or FakeLlm(),
        audio=audio_svc,
        minimax_tts=minimax or FakeMinimax(),
        ffmpeg_path=bin_ffmpeg if bin_ffmpeg.exists() else None,
        provider="minimax",
    )

    def fake_ffmpeg_concat(self, seg_files, output_path):
        output_path.write_bytes(seg_files[0].read_bytes())
    p._ffmpeg_concat = fake_ffmpeg_concat.__get__(p, TtsPipeline)
    return p


async def _collect(gen):
    return [ev async for ev in gen]


# ---- _run_minimax_pipeline --------------------------------------------------


async def test_run_minimax_pipeline_short_text_no_split(tmp_path: Path):
    """短文本不切分，task_dir 下写 <task_id>.mp3。"""
    audio_svc = AudioStorageService(tmp_path)
    p = _pipeline_with_audio(audio_svc)
    p._task_id = "tid_short"
    p._task_date_str = "20260704"
    events = await _collect(p.run_from_normalized(
        "短文本", filename="x.md", voice_id="male-qn-qingse",
    ))
    done = [e for e in events if e.stage == "done"][0]
    # task_dir/<task_id>.mp3 已写
    mp3 = audio_svc.task_file_path("tid_short", "tid_short.mp3", date_str="20260704")
    assert mp3.exists()
    # done event subtitle_status='ok'（有 cues）
    assert done.subtitle_status == "ok"


async def test_run_minimax_pipeline_writes_split_md_and_mp3(tmp_path: Path):
    """pre_split_chunks 非空 → 逐段写 split_<N>.md / split_<N>.mp3 / split_<N>.SRT。"""
    audio_svc = AudioStorageService(tmp_path)
    p = _pipeline_with_audio(audio_svc)
    p._task_id = "tid_split"
    p._task_date_str = "20260704"
    events = await _collect(p.run_from_normalized(
        "normalized text", filename="x.md", voice_id="male-qn-qingse",
        pre_split_chunks=["段落A" * 10, "段落B" * 10],
    ))
    task_dir = audio_svc.task_dir("tid_split", date_str="20260704")
    # normalized.md 必存在
    assert (task_dir / "normalized.md").exists()
    # split_<N>.md / split_<N>.mp3 / split_<N>.SRT 都写
    for n in (1, 2):
        assert (task_dir / f"split_{n}.md").exists()
        assert (task_dir / f"split_{n}.mp3").exists()
        assert (task_dir / f"split_{n}.SRT").exists()
    done = [e for e in events if e.stage == "done"][0]
    assert done.stage == "done"


async def test_done_event_subtitle_pending_when_fetch_fails(tmp_path: Path):
    """字幕拉取失败：done event subtitle_status='pending'，音频仍生成。"""
    audio_svc = AudioStorageService(tmp_path)
    p = _pipeline_with_audio(
        audio_svc,
        minimax=FakeMinimax(subtitle_error="OSS 404", cues=[]),
    )
    p._task_id = "tid_sub"
    p._task_date_str = "20260704"
    events = await _collect(p.run_from_normalized(
        "测试文本", filename="x.md", voice_id="male-qn-qingse",
    ))
    final = [e for e in events if e.stage == "done"][0]
    assert final.subtitle_status == "pending"
    assert "404" in (final.subtitle_error or "")
    # 音频仍可用
    assert audio_svc.task_file_path("tid_sub", "tid_sub.mp3", date_str="20260704").exists()


async def test_done_event_subtitle_ok_when_cues_present(tmp_path: Path):
    """字幕 cues 成功 → done event subtitle_status='ok'。"""
    audio_svc = AudioStorageService(tmp_path)
    p = _pipeline_with_audio(audio_svc)
    p._task_id = "tid_ok"
    p._task_date_str = "20260704"
    events = await _collect(p.run_from_normalized(
        "测试文本", filename="x.md", voice_id="male-qn-qingse",
    ))
    final = [e for e in events if e.stage == "done"][0]
    assert final.subtitle_status == "ok"


# ---- TaskManager 字幕失败 → subtitle_pending -------------------------------


async def test_task_manager_subtitle_pending_marks_task(tmp_path: Path):
    """TaskManager._do_convert 监听 done event subtitle_status='pending' → 标 subtitle_pending。"""
    out = tmp_path / "out"
    (out / "uploads").mkdir(parents=True, exist_ok=True)
    db = out / "lib.db"
    audio_svc = AudioStorageService(out)
    task_store = TaskStore(db)

    p = _pipeline_with_audio(
        audio_svc,
        minimax=FakeMinimax(subtitle_error="OSS 404", cues=[]),
    )
    mgr = TaskManager(
        pipeline=p,
        task_store=task_store,
        audio_storage=audio_svc,
        llm=FakeLlm(),
    )
    task_id = mgr.create_task(b"# test", filename="x.md")
    # 跳过标准化 + 拆分，把任务推进到 ready_to_convert
    mgr.skip_normalize(task_id)
    mgr.skip_split(task_id)
    mgr.convert_task(task_id)
    await asyncio.sleep(0.5)

    final = task_store.get(task_id)
    assert final is not None
    assert final.status == TASK_STATUS_SUBTITLE_PENDING
    # task_dir/<task_id>.mp3 已落盘（音频可用）
    assert audio_svc.task_file_path(task_id, f"{task_id}.mp3",
                                    date_str=final.date_str).exists()


# ---- retry_task 阶段感知 ---------------------------------------------------


def _mgr_with_task(task: TaskRecord, *, audio=None, llm=None):
    """构造 TaskManager + 已有 task 的简单 helper。"""
    out = Path(audio._root) if audio else Path(tempfile.mkdtemp())
    (out / "uploads").mkdir(parents=True, exist_ok=True)
    # 写原始 md 到 task_dir/<task_id>/<task_id>.md（v4 位置）
    audio_svc = audio or AudioStorageService(out)
    date_str = task.date_str or task_date_str()
    audio_svc.task_dir(task.task_id, date_str=date_str)
    (audio_svc.task_file_path(task.task_id, f"{task.task_id}.md",
                              date_str=date_str)).write_text("# test", encoding="utf-8")
    db = out / "lib.db"
    task_store = TaskStore(db)
    task_store.insert(task)

    p = _pipeline_with_audio(audio_svc)
    mgr = TaskManager(
        pipeline=p,
        task_store=task_store,
        audio_storage=audio_svc,
        llm=llm or FakeLlm(),
    )
    return mgr, task_store, audio_svc


import tempfile  # noqa: E402


def test_retry_subtitle_pending_triggers_convert_again(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_SUBTITLE_PENDING, current_stage="subtitle_pending",
        progress=0.95, message="字幕待重试",
        error="subtitle_pending: OSS 404",
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        normalized_text="测试文本",
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    mgr.convert_task = MagicMock(return_value=True)
    ok = mgr.retry_task("t1")
    assert ok is True
    rec = store.get("t1")
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    assert rec.retry_count == 1
    mgr.convert_task.assert_called_once_with("t1")


def test_retry_failed_retryable_no_normalized_text_resets_to_draft(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_FAILED_RETRYABLE, current_stage="llm_normalize",
        progress=0.10, message="失败", error="LLM timeout",
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        original_md_path="20260704/t1/t1.md",
        local_clean_text="原始清洗文本",
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    mgr.normalize_task = MagicMock(return_value=True)
    ok = mgr.retry_task("t1")
    assert ok is True
    rec = store.get("t1")
    assert rec.status == TASK_STATUS_DRAFT
    assert rec.retry_count == 1
    mgr.normalize_task.assert_called_once_with("t1")


def test_retry_failed_retryable_with_normalized_no_chunks_triggers_split(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_FAILED_RETRYABLE, current_stage="tts_synthesize",
        progress=0.70, message="失败", error="tts boom",
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        original_md_path="20260704/t1/t1.md",
        normalized_text="已标准化的文本",
        split_prompt="按章节划分",
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    mgr.split_task = MagicMock(return_value=True)
    ok = mgr.retry_task("t1")
    assert ok is True
    rec = store.get("t1")
    assert rec.status == TASK_STATUS_READY_TO_SPLIT
    mgr.split_task.assert_called_once_with("t1", "按章节划分")


def test_retry_failed_retryable_with_chunks_triggers_convert(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_FAILED_RETRYABLE, current_stage="tts_synthesize",
        progress=0.70, message="失败", error="tts boom",
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        original_md_path="20260704/t1/t1.md",
        normalized_text="已标准化的文本",
        split_chunks=json.dumps(["chunk1", "chunk2"], ensure_ascii=False),
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    mgr.convert_task = MagicMock(return_value=True)
    ok = mgr.retry_task("t1")
    assert ok is True
    rec = store.get("t1")
    assert rec.status == TASK_STATUS_READY_TO_CONVERT
    mgr.convert_task.assert_called_once_with("t1")


def test_retry_done_returns_false(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_DONE, current_stage="done", progress=1.0,
        message="完成", error=None,
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        original_md_path="20260704/t1/t1.md",
        normalized_text="...",
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    assert mgr.retry_task("t1") is False


def test_retry_md_missing_marks_error(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task = TaskRecord(
        task_id="t1", filename="x.md", voice_id="male-qn-qingse",
        status=TASK_STATUS_FAILED_RETRYABLE, current_stage="llm_normalize",
        progress=0.10, message="失败", error="x",
        date_str="20260704",
        created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z",
        original_md_path="20260704/t1/t1.md",
        normalized_text="...",
    )
    mgr, store, _ = _mgr_with_task(task, audio=audio_svc)
    # 删 task_dir 模拟 md 丢失
    import shutil
    shutil.rmtree(audio_svc.task_dir("t1", date_str="20260704"))

    ok = mgr.retry_task("t1")
    assert ok is False
    rec = store.get("t1")
    assert rec.status == TASK_STATUS_ERROR


# ---- TASK_RETRYABLE_STATUSES 包含 subtitle_pending ---------------------------


def test_retryable_statuses_include_subtitle_pending():
    from app.services.audio_storage import TASK_RETRYABLE_STATUSES
    assert TASK_STATUS_SUBTITLE_PENDING in TASK_RETRYABLE_STATUSES
    assert TASK_STATUS_FAILED_RETRYABLE in TASK_RETRYABLE_STATUSES
    assert TASK_STATUS_ERROR in TASK_RETRYABLE_STATUSES