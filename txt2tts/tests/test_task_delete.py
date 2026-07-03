"""Unit tests for the delete-task feature.

覆盖：
    * ``AudioStorageService.save`` 写到 ``audio/<audio_id>.mp3``（v2 布局）
    * ``AudioStorageService.resolve`` 优先新约定，兼容旧日期目录与 rglob 兜底
    * ``AudioStorageService.promote_artifacts`` 把 chunks/segments/uploads.md 搬到 _artifacts
    * ``AudioStorageService.delete_task_files`` 按 keep_final_audio 区分删/留
    * ``TaskStore.delete`` 与 ``LibraryStore.delete``
    * ``TaskManager.delete_task`` 全路径（done 保留 / 其它彻底删）
    * ``DELETE /api/tasks/{id}`` HTTP 路由（通过直接调 endpoint 函数，monkeypatch 依赖）
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from app.services.audio_storage import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED_RETRYABLE,
    AudioRecord,
    AudioStorageService,
    LibraryStore,
    TaskRecord,
    TaskStore,
)
from app.services.pipeline import ProgressEvent
from app.services.task_manager import TaskManager


# ---- helpers --------------------------------------------------------------


def _task(task_id: str = "abc123", *, status: str = TASK_STATUS_DONE,
          audio_id: str = "aud456",
          provider: str = "mimo",
          updated_at: str = "2026-07-02T00:00:00Z") -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        filename=f"{task_id}.md",
        voice_id="mimo_default",
        status=status,
        current_stage="done",
        progress=1.0,
        message="完成",
        audio_id=audio_id,
        error=None,
        created_at=updated_at,
        updated_at=updated_at,
        original_md_path=f"uploads/{task_id}.md",
        retry_count=0,
        provider=provider,
    )


def _audio_record(audio_id: str = "aud456") -> AudioRecord:
    return AudioRecord(
        audio_id=audio_id,
        original_filename="test.md",
        original_md="# x",
        normalized_md="x",
        voice_id="mimo_default",
        duration_sec=None,
        byte_size=1024,
        created_at="2026-07-02T00:00:00Z",
        provider="mimo",
    )


# ---- AudioStorageService: save / resolve / resolve_lyrics ----------------


def test_save_writes_to_audio_dir(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    stored = svc.save(b"hello-mp3-bytes")
    assert stored.file_path.parent == svc.audio_dir
    assert stored.file_path.name == f"{stored.audio_id}.mp3"
    assert stored.file_path.read_bytes() == b"hello-mp3-bytes"


def test_resolve_prefers_audio_dir(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    stored = svc.save(b"v2")
    # 在旧位置放一个同名假文件，确认 resolve 优先返回 v2
    legacy = tmp_path / "2026-07-01" / f"{stored.audio_id}.mp3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    found = svc.resolve(stored.audio_id)
    assert found == stored.file_path


def test_resolve_falls_back_to_legacy_date_dir(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    aid = "deadbeef"
    legacy = tmp_path / "2026-06-30" / f"{aid}.mp3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old")
    assert svc.resolve(aid) == legacy


def test_resolve_returns_none_for_unknown(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    assert svc.resolve("nonexistent") is None


def test_resolve_lyrics_prefers_artifacts_dir(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    aid = "abc123"
    art_dir = svc.artifacts_dir(aid)
    art_dir.mkdir(parents=True)
    new_lrc = art_dir / f"{aid}.lrc"
    new_lrc.write_text("[00:00.00]hi", encoding="utf-8")
    # 在旧位置也放一个，resolve_lyrics 应优先新位置
    legacy = tmp_path / "lyrics" / f"{aid}.lrc"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[00:00.00]legacy", encoding="utf-8")
    assert svc.resolve_lyrics(aid) == new_lrc


def test_resolve_lyrics_falls_back_to_legacy(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    aid = "abc123"
    legacy = tmp_path / "lyrics" / f"{aid}.lrc"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[00:00.00]legacy", encoding="utf-8")
    assert svc.resolve_lyrics(aid) == legacy


# ---- AudioStorageService: promote_artifacts ------------------------------


def test_promote_artifacts_moves_chunks_segments_uploads(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    task_id = "tid"
    audio_id = "aud"
    # 1) chunks/<task_id>/ 多个文件
    chunks = tmp_path / "chunks" / task_id
    chunks.mkdir(parents=True)
    (chunks / "001.md").write_text("a", encoding="utf-8")
    (chunks / "001.mp3").write_bytes(b"x")
    (chunks / "normalized.md").write_text("n", encoding="utf-8")
    (chunks / "final.mp3").write_bytes(b"final")
    # 2) segments/<task_id>/
    segs = tmp_path / "segments" / task_id
    segs.mkdir(parents=True)
    (segs / "0000.mp3").write_bytes(b"seg")
    # 3) uploads/<task_id>.md
    uploads = tmp_path / "uploads" / f"{task_id}.md"
    uploads.parent.mkdir(parents=True)
    uploads.write_text("# orig", encoding="utf-8")

    out = svc.promote_artifacts(task_id=task_id, audio_id=audio_id)
    assert out["moved"] >= 5  # 4 chunks + 1 seg + 1 upload = 6
    art = svc.artifacts_dir(audio_id)
    assert (art / "001.md").exists()
    assert (art / "001.mp3").exists()
    assert (art / "normalized.md").exists()
    assert (art / "final.mp3").exists()
    assert (art / "0000.mp3").exists()
    assert (art / f"{audio_id}.md").exists()
    assert (art / f"{audio_id}.md").read_text(encoding="utf-8") == "# orig"
    # 源目录应被清空
    assert not chunks.exists()
    assert not segs.exists()
    assert not uploads.exists()


def test_promote_artifacts_handles_missing_dirs(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    out = svc.promote_artifacts(task_id="ghost", audio_id="aud")
    assert out["moved"] == 0
    # 不应抛错；dest 创建
    assert svc.artifacts_dir("aud").exists()


def test_promote_artifacts_consolidates_legacy_mp3(tmp_path: Path):
    """旧日期目录里的 mp3 也被搬到 audio/。"""
    svc = AudioStorageService(tmp_path)
    aid = "legacy-aud"
    legacy = tmp_path / "2026-06-30" / f"{aid}.mp3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old")
    out = svc.promote_artifacts(task_id="t", audio_id=aid)
    assert (svc.audio_dir / f"{aid}.mp3").exists()
    assert not legacy.exists()
    assert out["moved"] >= 1


# ---- AudioStorageService: delete_task_files ------------------------------


def test_delete_task_files_done_keeps_final_audio(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    tid, aid = "t1", "a1"
    # 准备文件
    (tmp_path / "uploads" / f"{tid}.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / f"{tid}.md").write_text("# x", encoding="utf-8")
    chunks = tmp_path / "chunks" / tid
    chunks.mkdir(parents=True)
    (chunks / "001.md").write_text("a", encoding="utf-8")
    art = svc.artifacts_dir(aid)
    art.mkdir(parents=True)
    (art / "001.md").write_text("a", encoding="utf-8")
    (art / "normalized.md").write_text("n", encoding="utf-8")
    final = svc.audio_dir / f"{aid}.mp3"
    final.write_bytes(b"FINAL")

    removed = svc.delete_task_files(
        task_id=tid, audio_id=aid, keep_final_audio=True,
    )
    # uploads + chunks 删了
    assert not (tmp_path / "uploads" / f"{tid}.md").exists()
    assert not chunks.exists()
    # 成品 + artifacts 保留
    assert final.exists()
    assert art.exists()
    assert (art / "001.md").exists()
    assert removed["uploads"] == 1
    assert removed["chunks"] == 1
    assert removed["final_mp3"] == 0
    assert removed["artifacts"] == 0


def test_delete_task_files_non_done_removes_everything(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    tid, aid = "t2", "a2"
    (tmp_path / "uploads" / f"{tid}.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / f"{tid}.md").write_text("# x", encoding="utf-8")
    art = svc.artifacts_dir(aid)
    art.mkdir(parents=True)
    (art / "001.md").write_text("a", encoding="utf-8")
    final = svc.audio_dir / f"{aid}.mp3"
    final.write_bytes(b"FINAL")
    # 兼容旧路径
    legacy_lyrics = tmp_path / "lyrics" / f"{aid}.lrc"
    legacy_lyrics.parent.mkdir(parents=True)
    legacy_lyrics.write_text("lrc", encoding="utf-8")

    removed = svc.delete_task_files(
        task_id=tid, audio_id=aid, keep_final_audio=False,
    )
    assert not (tmp_path / "uploads" / f"{tid}.md").exists()
    assert not art.exists()
    assert not final.exists()
    assert not legacy_lyrics.exists()
    assert removed["uploads"] == 1
    assert removed["final_mp3"] == 1
    assert removed["artifacts"] == 1


def test_delete_task_files_no_audio_id(tmp_path: Path):
    """任务没成功过（audio_id=None）→ 只清 uploads / chunks / segments。"""
    svc = AudioStorageService(tmp_path)
    tid = "t3"
    (tmp_path / "uploads" / f"{tid}.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / f"{tid}.md").write_text("# x", encoding="utf-8")
    chunks = tmp_path / "chunks" / tid
    chunks.mkdir(parents=True)
    (chunks / "x.md").write_text("a", encoding="utf-8")
    removed = svc.delete_task_files(task_id=tid, audio_id=None, keep_final_audio=False)
    assert not (tmp_path / "uploads" / f"{tid}.md").exists()
    assert not chunks.exists()
    assert removed["final_mp3"] == 0
    assert removed["artifacts"] == 0


# ---- TaskStore.delete / LibraryStore.delete -----------------------------


def test_task_store_delete(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("x"))
    assert store.delete("x") is True
    assert store.get("x") is None
    assert store.delete("x") is False


def test_library_store_delete(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    store.insert(_audio_record("a1"))
    assert store.delete("a1") is True
    assert store.get("a1") is None
    assert store.delete("a1") is False


# ---- TaskManager.delete_task ----------------------------------------------


def _fake_pipeline_run(events: list[ProgressEvent]):
    async def gen(*args, **kwargs) -> AsyncIterator[ProgressEvent]:
        for ev in events:
            yield ev
    return gen


async def test_task_manager_delete_task_done_keeps_final_audio(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    library = LibraryStore(tmp_path / "lib.db")
    audio_svc = AudioStorageService(tmp_path)
    pipeline = MagicMock()
    pipeline._provider = "mimo"
    mgr = TaskManager(
        pipeline=pipeline, task_store=store,
        uploads_dir=tmp_path / "uploads",
        audio_storage=audio_svc, library=library,
    )
    # 准备一个 done 任务的完整文件分布
    tid, aid = "done-tid", "done-aud"
    (tmp_path / "uploads" / f"{tid}.md").write_text("# x", encoding="utf-8")
    chunks = tmp_path / "chunks" / tid
    chunks.mkdir(parents=True)
    (chunks / "001.md").write_text("a", encoding="utf-8")
    art = audio_svc.artifacts_dir(aid)
    art.mkdir(parents=True)
    (art / "001.md").write_text("a", encoding="utf-8")
    final = audio_svc.audio_dir / f"{aid}.mp3"
    final.write_bytes(b"FINAL")
    # 入库
    store.insert(_task(tid, status=TASK_STATUS_DONE, audio_id=aid))
    library.insert(_audio_record(aid))

    result = mgr.delete_task(tid)
    assert result["found"] is True
    assert result["kept_final_audio"] is True
    assert result["tasks_row_deleted"] is True
    assert result["library_row_deleted"] is True
    # 文件检查：uploads/chunks 没了；artifacts + audio/<aid>.mp3 保留
    assert not (tmp_path / "uploads" / f"{tid}.md").exists()
    assert not chunks.exists()
    assert final.exists()
    assert (art / "001.md").exists()
    # DB 行没了
    assert store.get(tid) is None
    assert library.get(aid) is None


async def test_task_manager_delete_task_failed_clears_everything(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    library = LibraryStore(tmp_path / "lib.db")
    audio_svc = AudioStorageService(tmp_path)
    pipeline = MagicMock()
    pipeline._provider = "mimo"
    mgr = TaskManager(
        pipeline=pipeline, task_store=store,
        uploads_dir=tmp_path / "uploads",
        audio_storage=audio_svc, library=library,
    )
    tid, aid = "fail-tid", "fail-aud"
    # failed_retryable 任务通常没有 audio_id；这里测一下 audio_id 为 None 的分支
    store.insert(_task(tid, status=TASK_STATUS_FAILED_RETRYABLE, audio_id=None))
    (tmp_path / "uploads" / f"{tid}.md").write_text("# x", encoding="utf-8")
    chunks = tmp_path / "chunks" / tid
    chunks.mkdir(parents=True)
    (chunks / "x.md").write_text("a", encoding="utf-8")

    result = mgr.delete_task(tid)
    assert result["found"] is True
    assert result["kept_final_audio"] is False
    assert result["tasks_row_deleted"] is True
    assert result["library_row_deleted"] is False  # audio_id=None 没 library 行
    assert not (tmp_path / "uploads" / f"{tid}.md").exists()
    assert not chunks.exists()
    assert store.get(tid) is None


def test_task_manager_delete_task_missing(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    audio_svc = AudioStorageService(tmp_path)
    pipeline = MagicMock()
    pipeline._provider = "mimo"
    mgr = TaskManager(
        pipeline=pipeline, task_store=store,
        uploads_dir=tmp_path / "uploads",
        audio_storage=audio_svc,
    )
    result = mgr.delete_task("nope")
    assert result["found"] is False


# ---- DELETE /api/tasks/{id} HTTP 路由 ------------------------------------


def test_delete_task_http_done_returns_kept_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真 HTTP 路径：done 任务被删后响应里 kept_final_audio=True，audio 文件仍在。"""
    # 直接 monkeypatch router 模块里的 4 个依赖，绕开 lifespan。
    from app.routers import tts as tts_router
    store = TaskStore(tmp_path / "tasks.db")
    library = LibraryStore(tmp_path / "lib.db")
    audio_svc = AudioStorageService(tmp_path)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    pipeline = MagicMock()
    pipeline._provider = "mimo"
    mgr = TaskManager(
        pipeline=pipeline,
        task_store=store,
        uploads_dir=uploads_dir,
        audio_storage=audio_svc,
        library=library,
    )
    monkeypatch.setattr(tts_router, "_task_store", store)
    monkeypatch.setattr(tts_router, "_library", library)
    monkeypatch.setattr(tts_router, "_audio_storage", audio_svc)
    monkeypatch.setattr(tts_router, "_pipelines", {"mimo": pipeline})
    monkeypatch.setattr(tts_router, "_active_provider", "mimo")
    monkeypatch.setattr(tts_router, "_task_manager", mgr)

    tid = "http-done-tid"
    aid = "http-done-aud"
    store.insert(_task(tid, status=TASK_STATUS_DONE, audio_id=aid))
    library.insert(_audio_record(aid))
    (uploads_dir / f"{tid}.md").write_text("# x", encoding="utf-8")
    art = audio_svc.artifacts_dir(aid)
    art.mkdir(parents=True, exist_ok=True)
    (art / "x.md").write_text("a", encoding="utf-8")
    final = audio_svc.audio_dir / f"{aid}.mp3"
    final.write_bytes(b"FINAL")

    # 走 ASGI 直接调 router 的 endpoint function（不依赖 lifespan）
    from app.routers.tts import delete_task as delete_endpoint
    result = asyncio.run(delete_endpoint(tid))
    assert result.found is True
    assert result.kept_final_audio is True
    assert result.audio_id == aid
    assert result.tasks_row_deleted is True
    assert result.library_row_deleted is True
    # 文件分布
    assert final.exists()
    assert art.exists()
    assert not (uploads_dir / f"{tid}.md").exists()
    # 二次删 → 404
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException) as ei:
        asyncio.run(delete_endpoint(tid))
    assert ei.value.status_code == 404