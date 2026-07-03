"""Unit tests for the SQLite-backed TaskStore and TaskManager."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.audio_storage import TaskRecord, TaskStore
from app.services.pipeline import ProgressEvent
from app.services.task_manager import TaskManager


# ---- helpers --------------------------------------------------------------


def _task(task_id: str = "abc123", *, filename: str = "test.md",
          status: str = "pending", progress: float = 0.0,
          created_at: str = "2026-07-01T08:00:00Z",
          updated_at: str = "2026-07-01T08:00:00Z") -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        filename=filename,
        voice_id="mimo_default",
        status=status,
        current_stage=None,
        progress=progress,
        message="等待处理…",
        audio_id=None,
        error=None,
        created_at=created_at,
        updated_at=updated_at,
    )


# ---- TaskStore tests -----------------------------------------------------


def test_task_store_empty_list(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    items, total = store.list_page(page=1, size=10)
    assert items == []
    assert total == 0


def test_task_store_insert_and_list(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("aaa"))
    store.insert(_task("bbb", filename="doc2.md", created_at="2026-07-01T09:00:00Z",
                       updated_at="2026-07-01T09:00:00Z"))
    items, total = store.list_page(page=1, size=10)
    assert total == 2
    # 降序：bbb 在前
    assert items[0].task_id == "bbb"
    assert items[1].task_id == "aaa"


def test_task_store_get_existing(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("xyz"))
    record = store.get("xyz")
    assert record is not None
    assert record.filename == "test.md"
    assert record.status == "pending"


def test_task_store_get_missing(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    assert store.get("nonexistent") is None


def test_task_store_update_progress(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("t1"))

    # 更新 status + progress
    ok = store.update_progress("t1", status="processing", progress=0.35, message="M3 标准化中…")
    assert ok is True

    record = store.get("t1")
    assert record.status == "processing"
    assert record.progress == 0.35
    assert record.message == "M3 标准化中…"
    # updated_at 应该变了
    assert record.updated_at != record.created_at


def test_task_store_update_progress_nonexistent(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    ok = store.update_progress("nope", status="done")
    assert ok is False


def test_task_store_update_partial(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("t2"))

    # 只更新 message，其他不变
    store.update_progress("t2", message="新消息")
    record = store.get("t2")
    assert record.status == "pending"  # 未改
    assert record.message == "新消息"
    assert record.progress == 0.0     # 未改


def test_task_store_pagination(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    for i in range(5):
        store.insert(_task(f"t{i:02d}", filename=f"f{i}.md",
                           created_at=f"2026-07-01T0{i}:00:00Z",
                           updated_at=f"2026-07-01T0{i}:00:00Z"))

    items, total = store.list_page(page=1, size=2)
    assert total == 5
    assert len(items) == 2
    # 第一页是最后两个
    assert items[0].task_id == "t04"
    assert items[1].task_id == "t03"

    items2, _ = store.list_page(page=2, size=2)
    assert len(items2) == 2
    assert items2[0].task_id == "t02"


# ---- provider 字段 ------------------------------------------------------


def test_task_store_insert_and_get_provider(tmp_path: Path):
    """TaskRecord.provider 字段必须能写入 tasks 表并读出。"""
    store = TaskStore(tmp_path / "tasks.db")
    rec = TaskRecord(
        task_id="pp1",
        filename="demo.md",
        voice_id="v",
        status="pending",
        current_stage=None,
        progress=0.0,
        message="等待处理…",
        audio_id=None,
        error=None,
        created_at="2026-07-02T00:00:00Z",
        updated_at="2026-07-02T00:00:00Z",
        original_md_path="uploads/pp1.md",
        retry_count=0,
        provider="mimo",
    )
    store.insert(rec)
    got = store.get("pp1")
    assert got is not None
    assert got.provider == "mimo"

    # list_page 也能读出来
    items, _ = store.list_page(page=1, size=10)
    assert items[0].provider == "mimo"


def test_task_store_provider_roundtrip_edge(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    rec = TaskRecord(
        task_id="ee1",
        filename="e.md",
        voice_id="v",
        status="pending",
        current_stage=None,
        progress=0.0,
        message="",
        audio_id=None,
        error=None,
        created_at="2026-07-02T00:00:00Z",
        updated_at="2026-07-02T00:00:00Z",
        provider="edge",
    )
    store.insert(rec)
    assert store.get("ee1").provider == "edge"


def test_task_store_provider_default_none_when_not_set(tmp_path: Path):
    """没传 provider → 读出来为 None（旧库兼容 / 未指定）。"""
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("xx"))  # helper 不带 provider
    got = store.get("xx")
    assert got is not None
    assert got.provider is None


async def test_task_manager_uses_pipeline_provider(tmp_path: Path):
    """TaskManager.create_task 写入的 provider 应来自 pipeline._provider。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    pipeline._provider = "edge"
    pipeline.run = _fake_pipeline_run([
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="done", progress=1.0, message="完成", audio_id="aud"),
    ])
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=tmp_path / "uploads")
    task_id = mgr.create_task(b"# x", filename="x.md")
    await asyncio.sleep(0.2)
    rec = store.get(task_id)
    assert rec.provider == "edge"


# ---- TaskManager tests (mock pipeline) -----------------------------------


def _fake_pipeline_run(events: list[ProgressEvent]):
    """返回一个 async generator，逐个产出 events。"""
    async def gen(*args, **kwargs) -> AsyncIterator[ProgressEvent]:
        for ev in events:
            yield ev
    return gen


async def test_task_manager_create_and_run(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="done", progress=1.0, message="完成", audio_id="aud123"),
    ]
    pipeline.run = _fake_pipeline_run(events)
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=tmp_path / "uploads")

    task_id = mgr.create_task(b"# hello", filename="test.md", voice_id="mimo_default")
    assert task_id  # 应该是一个非空 hex 字符串

    # 等待后台协程完成
    await asyncio.sleep(0.2)

    record = store.get(task_id)
    assert record is not None
    assert record.status == "done"
    assert record.audio_id == "aud123"
    assert record.progress == 1.0


async def test_task_manager_pipeline_error(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="error", progress=0.1, message="失败", error="boom"),
    ]
    pipeline.run = _fake_pipeline_run(events)
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=tmp_path / "uploads")

    task_id = mgr.create_task(b"bad content", filename="fail.md")
    await asyncio.sleep(0.2)

    record = store.get(task_id)
    assert record is not None
    assert record.status == "failed_retryable"
    assert record.original_md_path is not None
    assert record.error == "boom"


# ---- 重试相关 -----------------------------------------------------------


async def test_task_manager_persists_original_md_on_disk(tmp_path: Path):
    """上传后原始 md 必须在 outputs/uploads/<task_id>.md 落盘。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="done", progress=1.0, message="完成", audio_id="aud"),
    ]
    pipeline.run = _fake_pipeline_run(events)
    uploads = tmp_path / "uploads"
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=uploads)

    raw = b"# hello\n\nbody"
    task_id = mgr.create_task(raw, filename="demo.md")
    await asyncio.sleep(0.2)

    md_on_disk = uploads / f"{task_id}.md"
    assert md_on_disk.exists()
    assert md_on_disk.read_bytes() == raw
    record = store.get(task_id)
    # Windows / POSIX 路径分隔符差异：用 Path 归一化比较
    assert Path(record.original_md_path).as_posix() == f"uploads/{task_id}.md"
    assert record.retry_count == 0


async def test_task_manager_retry_succeeds_after_failure(tmp_path: Path):
    """第一次失败 → status=failed_retryable；重试成功 → status=done。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    uploads = tmp_path / "uploads"

    # 第一次：pipeline yield error
    fail_events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="error", progress=0.5, message="失败", error="network"),
    ]
    pipeline.run = _fake_pipeline_run(fail_events)
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=uploads)
    task_id = mgr.create_task(b"# original", filename="demo.md")
    await asyncio.sleep(0.2)
    record = store.get(task_id)
    assert record.status == "failed_retryable"
    assert record.retry_count == 0
    assert record.error == "network"

    # 重试：pipeline yield done
    done_events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="done", progress=1.0, message="完成", audio_id="aud_new"),
    ]
    pipeline.run = _fake_pipeline_run(done_events)
    ok = mgr.retry_task(task_id)
    assert ok is True
    await asyncio.sleep(0.2)

    record = store.get(task_id)
    assert record.status == "done"
    assert record.audio_id == "aud_new"
    assert record.retry_count == 1
    assert record.error is None or record.error == "" or record.error is None


async def test_task_manager_retry_returns_false_when_status_not_failed(tmp_path: Path):
    """进行中 / 已完成的任务不允许重试。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    pipeline.run = _fake_pipeline_run([
        ProgressEvent(stage="start", progress=0.0, message="开始"),
    ])
    uploads = tmp_path / "uploads"
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=uploads)

    task_id = mgr.create_task(b"# x", filename="x.md")
    await asyncio.sleep(0.1)

    # 此时 status 是 pending 或 processing
    ok = mgr.retry_task(task_id)
    assert ok is False


async def test_task_manager_retry_returns_false_when_md_missing(tmp_path: Path):
    """原始 md 文件被删 → retry 时把 task 标记为不可重试 error 并返回 False。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()
    fail_events = [
        ProgressEvent(stage="start", progress=0.0, message="开始"),
        ProgressEvent(stage="error", progress=0.5, message="失败", error="api timeout"),
    ]
    pipeline.run = _fake_pipeline_run(fail_events)
    uploads = tmp_path / "uploads"
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=uploads)

    task_id = mgr.create_task(b"# original", filename="demo.md")
    await asyncio.sleep(0.2)
    assert store.get(task_id).status == "failed_retryable"

    # 删掉原始 md
    md_path = uploads / f"{task_id}.md"
    md_path.unlink()

    # 重试
    ok = mgr.retry_task(task_id)
    assert ok is False
    record = store.get(task_id)
    assert record.status == "error"
    assert "丢失" in record.message or "missing" in (record.error or "")


async def test_task_manager_coroutine_exception_marked_retryable(tmp_path: Path):
    """pipeline.run 本身抛异常（非 yield error）时，也应标记为 failed_retryable。"""
    store = TaskStore(tmp_path / "tasks.db")
    pipeline = MagicMock()

    async def boom(*args, **kwargs):
        if False:
            yield  # 标记为 generator
        raise RuntimeError("pipeline crashed hard")
        yield  # unreachable

    pipeline.run = boom
    uploads = tmp_path / "uploads"
    mgr = TaskManager(pipeline=pipeline, task_store=store, uploads_dir=uploads)

    task_id = mgr.create_task(b"# x", filename="x.md")
    await asyncio.sleep(0.2)

    record = store.get(task_id)
    assert record.status == "failed_retryable"
    assert "pipeline crashed hard" in (record.error or "")
