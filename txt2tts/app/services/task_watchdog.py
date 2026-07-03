"""后台任务看门狗（stall watchdog）。

任务背景：
    ``TaskManager._run_pipeline_from_memory`` 通过 ``asyncio.create_task``
    消费 ``pipeline.run`` 的 async generator。HTTP 调用层面已有
    ``request_timeout_sec``，但仍存在以下场景会让后台协程永远不返回：

    * M3 / MiMo 服务端 TCP 连接建立后无限等待（应用层 hang）
    * DNS 黑洞导致 SSL 握手长时间挂起但不被识别为超时
    * 中间链路设备故障，客户端卡在 ``await`` 上
    * 任何第三方 SDK 在用户层未抛 ``TimeoutError`` 的 bug

    上述情况下 ``TaskStore`` 里 ``status='processing'`` 的任务会永远停在
    同一个 ``current_stage``，前端 2s 轮询看到的进度条不再前进。

修复策略：
    FastAPI lifespan 启动时 ``asyncio.create_task`` 起一个看门狗协程，
    每 ``interval_sec`` 秒扫描一次 ``TaskStore.list_processing()``：
    若某个任务的 ``updated_at`` 与 ``now()`` 之差 > ``threshold_sec``，
    调用 ``TaskStore.mark_stalled`` 把它标为 ``failed_retryable``。

设计要点：
    * ``clock`` / ``threshold`` / ``interval`` 全部可注入，方便测试用 fake clock。
    * 只关心 ``status='processing'`` 的任务，已 done / error / failed_retryable 跳过。
    * shutdown 时 ``stop()`` 让循环退出，避免泄漏。
    * 与现有 FastAPI 单 worker 部署一致；多 worker 需要外部协调。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from app.services.audio_storage import (
    TASK_STATUS_PROCESSING,
    TaskStore,
)

logger = logging.getLogger(__name__)


# updated_at 在 TaskStore 里以 ISO-8601 字符串存储，UTC 带 'Z' 后缀。
# 这里集中处理解析，便于测试时把 ``_parse_iso`` patch 掉。
def _parse_iso(ts: str) -> float:
    """把 ISO-8601（带 'Z'）字符串解析为 POSIX 秒。容错：失败返回 0.0。"""
    if not ts:
        return 0.0
    try:
        # Python 3.11+ supports 'Z' directly; for 3.10 fallback replace.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


class StallWatchdog:
    """后台任务卡死检测器。

    使用方式（生产）::

        watchdog = StallWatchdog(
            task_store=task_store,
            threshold_sec=settings.task_stall_timeout_sec,
            interval_sec=settings.task_watchdog_interval_sec,
        )
        watchdog.start()        # 在 lifespan 中调用
        ...
        await watchdog.stop()   # 在 lifespan shutdown 中调用

    使用方式（测试）::

        watchdog = StallWatchdog(
            task_store=fake_store,
            threshold_sec=60,
            interval_sec=10,
            clock=lambda: fake_now,
            sleep=lambda s: asyncio.sleep(0),  # 跳过真实等待
        )
        await watchdog.tick()   # 单独跑一次扫描
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        threshold_sec: float,
        interval_sec: float = 10.0,
        enabled: bool = True,
        clock: Optional[Callable[[], float]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if threshold_sec <= 0:
            raise ValueError("threshold_sec must be > 0")
        if interval_sec <= 0:
            raise ValueError("interval_sec must be > 0")
        self._store = task_store
        self._threshold = float(threshold_sec)
        self._interval = float(interval_sec)
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self._sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """在主事件循环中起 watchdog 协程；幂等（重复调用不会重复启动）。"""
        if not self._enabled:
            logger.info("StallWatchdog disabled (enabled=False), skipping start")
            return
        if self._task is not None and not self._task.done():
            logger.debug("StallWatchdog already running")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="stall-watchdog")
        logger.info(
            "StallWatchdog started (threshold=%.0fs, interval=%.1fs)",
            self._threshold, self._interval,
        )

    async def stop(self) -> None:
        """取消 watchdog 协程并等待它退出；幂等。"""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._stop_event = None
        logger.info("StallWatchdog stopped")

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("StallWatchdog tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
            except asyncio.TimeoutError:
                continue

    # -- single scan (testable) ------------------------------------------

    def tick(self) -> int:
        """单次扫描；返回这次被标记失败的任务数。

        判定规则：对每个 status='processing' 的任务，
        若 ``now - parse(updated_at) > threshold``，调用 ``mark_stalled``。
        """
        now = self._clock()
        records = self._store.list_processing()
        killed = 0
        for r in records:
            last_update = _parse_iso(r.updated_at)
            stall = now - last_update
            if stall > self._threshold:
                ok = self._store.mark_stalled(
                    r.task_id,
                    current_stage=r.current_stage,
                    stall_seconds=stall,
                    threshold_sec=self._threshold,
                )
                if ok:
                    killed += 1
                    logger.warning(
                        "StallWatchdog: task %s stalled at stage=%r for %.0fs (>%.0fs); "
                        "marked failed_retryable",
                        r.task_id, r.current_stage, stall, self._threshold,
                    )
        if killed:
            logger.info(
                "StallWatchdog tick done: %d task(s) marked failed_retryable "
                "(scanned %d processing)",
                killed, len(records),
            )
        return killed

    # -- introspection ----------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()