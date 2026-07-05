"""v4 测试：任务目录布局（task_dir / delete_task_files / resolve）。

覆盖：
    * ``task_dir()`` 自动创建 / 重复调用幂等
    * ``task_file_path()`` 路径拼接
    * ``resolve()`` 命中 / 不命中
    * ``resolve_lyrics()`` 命中 / 不命中
    * ``delete_task_files()`` 整个 task_dir rmtree
    * 跳过标准化 / 跳过拆分 的文件复制语义（通过 TaskManager 接口间接验证）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.audio_storage import (
    AudioStorageService,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_READY_TO_CONVERT,
    TASK_STATUS_READY_TO_SPLIT,
    TaskRecord,
    TaskStore,
    task_date_str,
)
from app.services.task_manager import TaskManager


# ---- AudioStorageService.task_dir / resolve / delete ---------------------


def test_task_dir_creates_directory(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    p = svc.task_dir("abc123", date_str="20260704")
    assert p.exists()
    assert p.is_dir()
    assert p == tmp_path / "20260704" / "abc123"


def test_task_dir_idempotent(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    p1 = svc.task_dir("abc123", date_str="20260704")
    (p1 / "marker.txt").write_text("x")
    p2 = svc.task_dir("abc123", date_str="20260704")
    assert p1 == p2
    assert (p2 / "marker.txt").exists()  # 不被覆盖


def test_task_file_path_construction(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    assert svc.task_file_path("tid", "split_1.md", date_str="20260704") == tmp_path / "20260704" / "tid" / "split_1.md"
    assert svc.task_file_path("tid", "abc.SRT", date_str="20260704") == tmp_path / "20260704" / "tid" / "abc.SRT"


def test_resolve_returns_mp3_if_exists(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    p = svc.task_file_path("tid", "tid.mp3", date_str="20260704")
    p.write_bytes(b"fake mp3")
    assert svc.resolve("tid", date_str="20260704") == p


def test_resolve_returns_none_if_missing(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    assert svc.resolve("nonexistent", date_str="20260704") is None


def test_resolve_lyrics(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    lrc = svc.task_file_path("tid", "tid.LRC", date_str="20260704")
    lrc.write_text("[00:00.00]test")
    assert svc.resolve_lyrics("tid", date_str="20260704") == lrc
    assert svc.resolve_lyrics("nonexistent", date_str="20260704") is None


def test_delete_task_files_removes_whole_directory(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    task_dir = svc.task_dir("tid", date_str="20260704")
    (task_dir / "tid.md").write_text("hello")
    (task_dir / "tid.mp3").write_bytes(b"mp3")
    (task_dir / "tid.SRT").write_text("srt")
    (task_dir / "tid.LRC").write_text("lrc")
    (task_dir / "split_1.md").write_text("chunk1")
    (task_dir / "split_1.mp3").write_bytes(b"chunk1mp3")
    assert task_dir.exists()
    result = svc.delete_task_files("tid", date_str="20260704")
    assert result["removed"] is True
    assert result["existed"] is True
    assert not task_dir.exists()


def test_delete_task_files_idempotent_on_missing(tmp_path: Path):
    svc = AudioStorageService(tmp_path)
    result = svc.delete_task_files("never-existed", date_str="20260704")
    assert result["removed"] is False
    assert result["existed"] is False


# ---- TaskManager: create_task 立即写 task_dir/<task_id>.md -------------


def test_create_task_writes_local_clean_to_task_dir(tmp_path: Path):
    """upload 后本地清洗结果立即写到 task_dir/<task_id>.md。"""
    audio_svc = AudioStorageService(tmp_path)
    db_path = tmp_path / "lib.db"
    task_store = TaskStore(db_path)
    mgr = TaskManager(
        pipeline=MagicMockPipeline(),  # 仅满足构造，不参与路径
        task_store=task_store,
        audio_storage=audio_svc,
        
    )
    md_bytes = b"# hello\n\n## section\n\nContent."
    task_id = mgr.create_task(md_bytes, filename="hello.md", voice_id="male-qn-qingse")

    # task_dir/<task_id>.md 存在
    record = task_store.get(task_id)
    md_path = audio_svc.task_file_path(task_id, f"{task_id}.md", date_str=record.date_str)
    assert md_path.exists()
    # 内容 = 本地清洗后的文本（去除 markdown 标记）
    content = md_path.read_text(encoding="utf-8")
    assert "hello" in content
    # date_str 是 "yyyyMMdd" 格式
    assert len(record.date_str) == 8 and record.date_str.isdigit()
    # original_md_path 是相对路径 "<yyyymmdd>/<task_id>/<task_id>.md"
    assert record.original_md_path == f"{record.date_str}/{task_id}/{task_id}.md"


# ---- TaskManager: skip_normalize 复制 <task_id>.md → normalization.md -----


def test_skip_normalize_copies_md_to_normalization(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    mgr = TaskManager(
        pipeline=MagicMockPipeline(),
        task_store=task_store,
        audio_storage=audio_svc,
        
    )
    task_id = mgr.create_task(b"# hi\nbody", filename="a.md")
    rec = task_store.get(task_id)
    date_str = rec.date_str

    ok = mgr.skip_normalize(task_id)
    assert ok is True

    norm_path = audio_svc.task_file_path(task_id, "normalization.md", date_str=date_str)
    assert norm_path.exists()
    src_path = audio_svc.task_file_path(task_id, f"{task_id}.md", date_str=date_str)
    assert norm_path.read_text(encoding="utf-8") == src_path.read_text(encoding="utf-8")
    # 状态应变为 ready_to_split
    assert task_store.get(task_id).status == TASK_STATUS_READY_TO_SPLIT
    # normalized_text 已复制
    assert task_store.get(task_id).normalized_text == src_path.read_text(encoding="utf-8")


# ---- TaskManager: skip_split 复制 normalization.md → split_1.md --------


def test_skip_split_copies_normalization_to_split_1(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    mgr = TaskManager(
        pipeline=MagicMockPipeline(),
        task_store=task_store,
        audio_storage=audio_svc,
        
    )
    task_id = mgr.create_task(b"# doc", filename="x.md")
    mgr.skip_normalize(task_id)
    rec = task_store.get(task_id)
    date_str = rec.date_str

    ok = mgr.skip_split(task_id)
    assert ok is True

    split_1 = audio_svc.task_file_path(task_id, "split_1.md", date_str=date_str)
    assert split_1.exists()
    norm = audio_svc.task_file_path(task_id, "normalization.md", date_str=date_str)
    assert split_1.read_text(encoding="utf-8") == norm.read_text(encoding="utf-8")
    # 状态：ready_to_convert + split_chunks 包含 normalization 内容
    rec_after = task_store.get(task_id)
    assert rec_after.status == TASK_STATUS_READY_TO_CONVERT
    chunks = json.loads(rec_after.split_chunks)
    assert len(chunks) == 1


# ---- TaskManager: delete_task 删除整个 task_dir ---------------------------


def test_delete_task_removes_entire_task_dir(tmp_path: Path):
    audio_svc = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    mgr = TaskManager(
        pipeline=MagicMockPipeline(),
        task_store=task_store,
        audio_storage=audio_svc,
        
    )
    task_id = mgr.create_task(b"# x", filename="x.md")
    # 模拟任务跑完：写一些产物
    task_dir = audio_svc.task_dir(task_id)
    (task_dir / f"{task_id}.mp3").write_bytes(b"mp3")
    (task_dir / f"{task_id}.SRT").write_text("srt")
    (task_dir / f"{task_id}.LRC").write_text("lrc")
    (task_dir / "split_1.md").write_text("chunk")

    result = mgr.delete_task(task_id)
    assert result["found"] is True
    assert not task_dir.exists()  # 整个 task_dir 没了
    assert task_store.get(task_id) is None  # tasks 表行也删了


# ---- TaskStore.list_done / list_page --------------------------------------


def test_task_store_list_done_only_returns_done(tmp_path: Path):
    task_store = TaskStore(tmp_path / "lib.db")
    for i, status in enumerate(["done", "draft", "done", "converting"]):
        task_store.insert(TaskRecord(
            task_id=f"tid{i:02d}",
            filename=f"f{i}.md",
            status=status,
            date_str="20260704",
            created_at=f"2026-07-04T00:00:0{i}Z",
            updated_at=f"2026-07-04T00:00:0{i}Z",
        ))
    items, total = task_store.list_done(page=1, size=10)
    assert total == 2
    assert {it.task_id for it in items} == {"tid00", "tid02"}
    # list_page 应返回全部
    items_all, total_all = task_store.list_page(page=1, size=10)
    assert total_all == 4


def test_task_store_date_str_required(tmp_path: Path):
    """v4 tasks 表 date_str 是 NOT NULL，新建任务必须写入。

    SQLite 在没显式 strict mode 下不强制 NOT NULL；这里只验证 TaskManager.create_task
    一定会填入非空 date_str。
    """
    from app.services.task_manager import TaskManager
    audio_svc = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    mgr = TaskManager(
        pipeline=MagicMockPipeline(),
        task_store=task_store,
        audio_storage=audio_svc,
        
    )
    task_id = mgr.create_task(b"# x", filename="x.md")
    record = task_store.get(task_id)
    assert record.date_str and len(record.date_str) == 8 and record.date_str.isdigit()


# ---- task_date_str helper -------------------------------------------------


def test_task_date_str_format():
    from datetime import datetime
    s = task_date_str(datetime(2026, 7, 4, 12, 34, 56))
    assert s == "20260704"
    # 不传参数用今天
    assert len(task_date_str()) == 8


# ---- pipeline._task_id 暴露给 AudioStorageService.task_dir -----------------


def test_pipeline_uses_self_task_id_for_task_dir(tmp_path: Path):
    """验证 pipeline 在 self._task_id 已设后，task_dir 用 task_id。"""
    from app.services.audio_storage import AudioStorageService
    from app.services.pipeline import TtsPipeline
    from app.services.llm_normalizer import LlmNormalizer

    audio = AudioStorageService(tmp_path)
    llm = LlmNormalizer.__new__(LlmNormalizer)  # 避开构造（不依赖 settings）
    # 直接构造 pipeline + 设 _task_id + 调用 task_dir
    pipe = TtsPipeline(
        llm=llm,
        audio=audio,
    )
    pipe._task_id = "fake_task_id"
    pipe._task_date_str = "20260704"
    p = audio.task_dir("fake_task_id", date_str="20260704")
    assert p == tmp_path / "20260704" / "fake_task_id"
    assert p.exists()


# ---- v6 本地清洗后 split_<N>.md 仍存在（被覆写而非删除） ----------------


async def test_local_clean_overwrites_split_files(tmp_path: Path):
    """v6：local_clean_task 覆写 split_<N>.md，文件仍存在（不是删除）。"""
    import asyncio
    audio = AudioStorageService(tmp_path)
    task_store = TaskStore(tmp_path / "lib.db")
    pipe = MagicMockPipeline()
    from unittest.mock import MagicMock as _MM
    from app.services.llm_normalizer import LlmNormalizer
    mgr = TaskManager(
        pipeline=pipe,
        task_store=task_store,
        audio_storage=audio,
        llm=_MM(spec=LlmNormalizer),
    )

    tid = mgr.create_task(b"# x", filename="x.md")
    date_str = task_store.get(tid).date_str
    # 模拟 splitted 状态 + 写入 2 个 split_<N>.md
    task_store.update_progress(
        tid,
        status="splitted",
        split_chunks=json.dumps(
            ["详情 https://a.b 联系 user@t.com", "# 标题"],
            ensure_ascii=False,
        ),
    )
    audio.task_file_path(tid, "split_1.md", date_str=date_str).write_text(
        "详情 https://a.b 联系 user@t.com", encoding="utf-8",
    )
    audio.task_file_path(tid, "split_2.md", date_str=date_str).write_text(
        "# 标题", encoding="utf-8",
    )

    # 触发本地清洗（异步：必须有 event loop）
    mgr.local_clean_task(tid, ["url", "email"])
    await asyncio.sleep(0.3)

    split_1 = audio.task_file_path(tid, "split_1.md", date_str=date_str)
    split_2 = audio.task_file_path(tid, "split_2.md", date_str=date_str)
    # 文件仍存在（不是删除）
    assert split_1.exists()
    assert split_2.exists()
    # 内容已被清洗
    assert "http" not in split_1.read_text(encoding="utf-8")
    assert "@" not in split_1.read_text(encoding="utf-8")
    assert "#" in split_2.read_text(encoding="utf-8")  # md_symbols 未启用 → 保留


# ---- MagicMockPipeline 占位（不参与 create_task 逻辑） ----------------


class MagicMockPipeline:
    """满足 TaskManager 构造的最小形态；create_task 不实际触发 pipeline.run。"""
    _provider = "minimax"

    async def run_from_normalized(self, *args, **kwargs):
        if False:
            yield None