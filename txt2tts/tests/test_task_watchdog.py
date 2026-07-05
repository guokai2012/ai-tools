"""Unit tests for ``StallWatchdog`` and ``TaskStore`` stall helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.audio_storage import (
    TASK_STATUS_CONVERTING,
    TASK_STATUS_FAILED_RETRYABLE,
    TaskRecord,
    TaskStore,
)
from app.services.task_watchdog import StallWatchdog, _parse_iso


# ---- helpers --------------------------------------------------------------


def _task(task_id: str, *, status: str = TASK_STATUS_CONVERTING,
          current_stage: str = "tts_synthesize",
          progress: float = 0.5,
          updated_at: str = "2026-07-02T00:00:00Z") -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        filename=f"{task_id}.md",
        voice_id="v",
        status=status,
        current_stage=current_stage,
        progress=progress,
        message="m",
        date_str="20260704",
        error=None,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- _parse_iso -----------------------------------------------------------


def test_parse_iso_z_suffix():
    ts = _parse_iso("2026-07-02T08:00:00Z")
    assert ts > 0
    # 同一时刻再调一次应一致
    assert _parse_iso("2026-07-02T08:00:00Z") == ts


def test_parse_iso_empty_returns_zero():
    assert _parse_iso("") == 0.0


def test_parse_iso_invalid_returns_zero():
    assert _parse_iso("not-a-date") == 0.0


# ---- TaskStore helpers ---------------------------------------------------


def test_task_store_list_processing_filters_status(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("a", status=TASK_STATUS_CONVERTING))
    store.insert(_task("b", status="done"))
    store.insert(_task("c", status=TASK_STATUS_CONVERTING))
    items = store.list_processing()
    assert {r.task_id for r in items} == {"a", "c"}


def test_task_store_mark_stalled(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    store.insert(_task("x"))
    ok = store.mark_stalled(
        "x", current_stage="tts_synthesize",
        stall_seconds=200.0, threshold_sec=180.0,
    )
    assert ok is True
    rec = store.get("x")
    assert rec.status == TASK_STATUS_FAILED_RETRYABLE
    # error 字段简洁描述（stage + stall 秒数）
    assert "tts_synthesize" in (rec.error or "")
    assert "200" in (rec.error or "")
    # message 字段含阈值（用户可见消息）
    assert "180" in (rec.message or "")
    assert "tts_synthesize" in (rec.message or "")
    # updated_at 应该被刷新（用于后续判定的"最近活动时间"）
    assert rec.updated_at != "2026-07-02T00:00:00Z"


def test_task_store_mark_stalled_missing_returns_false(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    assert store.mark_stalled(
        "nope", current_stage=None, stall_seconds=10, threshold_sec=5,
    ) is False


# ---- StallWatchdog.tick --------------------------------------------------


def test_watchdog_tick_marks_long_stalled(tmp_path: Path):
    """updated_at 早于 now-threshold 的 processing 任务会被标记失败。"""
    store = TaskStore(tmp_path / "tasks.db")
    # updated_at 固定在 epoch=0；fake clock 设到 200s 后
    old = "1970-01-01T00:00:00Z"
    store.insert(_task("stale", updated_at=old))
    fake_now = 200.0
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=10.0,
        clock=lambda: fake_now,
    )
    killed = wd.tick()
    assert killed == 1
    rec = store.get("stale")
    assert rec.status == TASK_STATUS_FAILED_RETRYABLE
    assert "stall" in (rec.error or "")


def test_watchdog_tick_ignores_fresh_tasks(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    now_iso = _now_iso()
    store.insert(_task("fresh", updated_at=now_iso))
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=10.0,
        clock=lambda: datetime.now(timezone.utc).timestamp(),
    )
    killed = wd.tick()
    assert killed == 0
    assert store.get("fresh").status == TASK_STATUS_CONVERTING


def test_watchdog_tick_ignores_non_processing(tmp_path: Path):
    """done / error / failed_retryable 不会被 watchdog 处理。"""
    store = TaskStore(tmp_path / "tasks.db")
    old = "1970-01-01T00:00:00Z"
    store.insert(_task("done", status="done", updated_at=old))
    store.insert(_task("err",  status="error", updated_at=old))
    store.insert(_task("fr",   status=TASK_STATUS_FAILED_RETRYABLE, updated_at=old))
    wd = StallWatchdog(
        task_store=store, threshold_sec=1.0,
        clock=lambda: 10_000.0,
    )
    killed = wd.tick()
    # list_processing 会过滤掉它们，所以 killed=0
    assert killed == 0
    assert store.get("done").status == "done"
    assert store.get("err").status == "error"
    assert store.get("fr").status == TASK_STATUS_FAILED_RETRYABLE


def test_watchdog_tick_threshold_boundary(tmp_path: Path):
    """stall == threshold 时不算卡死（严格大于）；stall == threshold+1 时算。"""
    store = TaskStore(tmp_path / "tasks.db")
    base = 1_000_000.0  # 任意基准秒
    old_ts = "1970-01-01T00:00:00Z"  # epoch = 0
    # 让 stall = threshold: 0 - 0 = 180 == 180 → 不触发
    store.insert(_task("boundary", updated_at=old_ts))
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=10.0,
        clock=lambda: 180.0,
    )
    assert wd.tick() == 0
    # 推进 1 秒：stall = 181 > 180 → 触发
    wd2 = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=10.0,
        clock=lambda: 181.0,
    )
    assert wd2.tick() == 1
    assert store.get("boundary").status == TASK_STATUS_FAILED_RETRYABLE


def test_watchdog_tick_multiple_tasks(tmp_path: Path):
    """多个 processing 任务中只有超时的会被标记。"""
    store = TaskStore(tmp_path / "tasks.db")
    fresh = _now_iso()
    store.insert(_task("stale1", updated_at="1970-01-01T00:00:00Z"))
    store.insert(_task("stale2", updated_at="1970-01-01T00:00:01Z"))
    store.insert(_task("alive", updated_at=fresh))
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0,
        clock=lambda: 1_000_000.0,
    )
    killed = wd.tick()
    assert killed == 2
    assert store.get("stale1").status == TASK_STATUS_FAILED_RETRYABLE
    assert store.get("stale2").status == TASK_STATUS_FAILED_RETRYABLE
    assert store.get("alive").status == TASK_STATUS_CONVERTING


def test_watchdog_invalid_threshold_raises(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    with pytest.raises(ValueError):
        StallWatchdog(task_store=store, threshold_sec=0)
    with pytest.raises(ValueError):
        StallWatchdog(task_store=store, threshold_sec=180.0, interval_sec=0)


# ---- StallWatchdog lifecycle (run / stop) --------------------------------


async def test_watchdog_start_stop_lifecycle(tmp_path: Path):
    """start() 后 _run 协程在跑；stop() 后退出。"""
    store = TaskStore(tmp_path / "tasks.db")
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=0.05,
        # sleep 用一个会立即等到 stop_event 的 fake
        sleep=lambda s: asyncio.sleep(0),
    )
    wd.start()
    assert wd.is_running
    await wd.stop()
    assert not wd.is_running


async def test_watchdog_start_idempotent(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=0.05,
        sleep=lambda s: asyncio.sleep(0),
    )
    wd.start()
    t1 = wd._task
    wd.start()  # 第二次不应重复创建
    t2 = wd._task
    assert t1 is t2
    await wd.stop()


async def test_watchdog_disabled_does_not_start(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    wd = StallWatchdog(
        task_store=store, threshold_sec=180.0, interval_sec=1.0,
        enabled=False,
    )
    wd.start()
    assert wd._task is None
    assert not wd.is_running


# asyncio 导入放最后避免顶部循环引用
import asyncio