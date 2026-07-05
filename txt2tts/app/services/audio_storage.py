"""Task directory layout + SQLite-backed stores.

v4 起（彻底重构）：
    ``<output_dir>/<yyyymmdd>/<task_id>/`` 一个任务一个目录，所有产物都在里面：
        <task_id>.md              本地清洗（upload 后立即落盘）
        normalization.md          M3 标准化；跳过时复制自 <task_id>.md
        split_<N>.md              M3 拆分；跳过时复制自 normalization.md → split_1.md
        split_<N>.mp3 + split_<N>.SRT  TTS 转换（minimax subtitle_file 解析 SRT）
        <task_id>.mp3 + <task_id>.SRT + <task_id>.LRC  ffmpeg 合并

SQLite 只保留两张表：
    - tasks:        任务元数据（含 date_str 字段）+ 听文档列表来源
    - app_settings: 运行时可切换的应用设置

彻底废弃：
    - audio_id / audio_records 表（听文档直接读 tasks）
    - LibraryStore / AudioRecord / StoredAudio dataclass
    - promote_artifacts / artifacts_dir / resolve_lyrics / audio_dir
    - LyricsService（转歌词功能下线）
    - uploads/ / chunks/ / segments/ / <YYYY-MM-DD>/ 历史路径
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---- Task directory 路径工具 ------------------------------------------------


def task_date_str(now: Optional[datetime] = None) -> str:
    """生成任务目录的日期段（连续 8 位数字）。"""
    return (now or datetime.now()).strftime("%Y%m%d")


class AudioStorageService:
    """任务目录布局 owner。

    所有写盘操作集中在 ``<output_dir>/<yyyymmdd>/<task_id>/`` 之下。
    """

    def __init__(self, output_dir: Path) -> None:
        self._root = Path(output_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        logger.info("AudioStorageService using directory: %s", self._root)

    # -- 路径构造 -----------------------------------------------------------

    def task_dir(self, task_id: str, *, date_str: Optional[str] = None) -> Path:
        """返回 ``<output>/<yyyymmdd>/<task_id>/``，自动创建。

        date_str 默认用今天（"yyyyMMdd"）；调用方传 TaskRecord.date_str 可确保
        删除/重试时跨日期也能找到原目录。
        """
        d = date_str or task_date_str()
        path = self._root / d / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def task_file_path(
        self, task_id: str, filename: str, *, date_str: Optional[str] = None,
    ) -> Path:
        return self.task_dir(task_id, date_str=date_str) / filename

    # -- 解析与删除 ---------------------------------------------------------

    def resolve(self, task_id: str, *, date_str: Optional[str] = None) -> Optional[Path]:
        """返回任务最终 mp3 的绝对路径（如果存在）。"""
        path = self.task_file_path(task_id, f"{task_id}.mp3", date_str=date_str)
        return path if path.exists() else None

    def resolve_lyrics(self, task_id: str, *, date_str: Optional[str] = None) -> Optional[Path]:
        """返回任务最终 LRC 的绝对路径（如果存在）。"""
        path = self.task_file_path(task_id, f"{task_id}.LRC", date_str=date_str)
        return path if path.exists() else None

    def delete_task_files(self, task_id: str, *, date_str: Optional[str] = None) -> dict:
        """直接 ``rmtree`` 整个任务目录。

        返回 {"removed": bool, "path": str, "existed": bool}。
        目录不存在时返回 removed=False, existed=False（幂等）。
        """
        d = date_str or task_date_str()
        path = self._root / d / task_id
        existed = path.exists()
        if not existed:
            return {"removed": False, "path": str(path), "existed": False}
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        logger.info("Deleted task directory: %s", path)
        return {"removed": True, "path": str(path), "existed": True}

    # -- 统计 ---------------------------------------------------------------

    def stats(self) -> dict:
        """outputs/ 占用统计（仅 mp3 体积）。"""
        mp3_paths = list(self._root.rglob("*.mp3"))
        total_bytes = sum(p.stat().st_size for p in mp3_paths if p.is_file())
        return {
            "output_dir": str(self._root),
            "mp3_count": len(mp3_paths),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / 1024 / 1024, 2),
        }


# ---- TaskRecord dataclass --------------------------------------------------


@dataclass(frozen=True)
class TaskRecord:
    """对应 tasks 表的 ORM 形态。"""
    task_id: str
    filename: str
    voice_id: Optional[str] = None
    status: str = "draft"
    current_stage: Optional[str] = None
    progress: float = 0.0
    message: str = ""
    date_str: str = ""                       # "yyyyMMdd"
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    retry_count: int = 0
    provider: Optional[str] = None            # "minimax" | "edge"
    original_md_path: Optional[str] = None   # "<yyyymmdd>/<task_id>/<task_id>.md"
    local_clean_text: Optional[str] = None
    normalized_text: Optional[str] = None
    split_prompt: Optional[str] = None
    split_chunks: Optional[str] = None       # JSON 数组字符串
    clean_options: Optional[str] = None      # JSON 数组字符串（清洗项 id）


# ---- Tasks: TaskStore -------------------------------------------------------


class TaskStore:
    """SQLite-backed 后台任务索引；同 library.db 单文件 + app_settings 表。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id           TEXT PRIMARY KEY,
        filename          TEXT NOT NULL,
        voice_id          TEXT,
        status            TEXT NOT NULL DEFAULT 'draft',
        current_stage     TEXT,
        progress          REAL NOT NULL DEFAULT 0.0,
        message           TEXT NOT NULL DEFAULT '',
        date_str          TEXT NOT NULL,
        error             TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        retry_count       INTEGER NOT NULL DEFAULT 0,
        provider          TEXT,
        original_md_path  TEXT,
        local_clean_text  TEXT,
        normalized_text   TEXT,
        split_prompt      TEXT,
        split_chunks      TEXT,
        clean_options     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    """

    _ALL_COLUMNS = [
        "task_id", "filename", "voice_id", "status", "current_stage",
        "progress", "message", "date_str", "error",
        "created_at", "updated_at", "retry_count", "provider",
        "original_md_path", "local_clean_text", "normalized_text", "split_prompt", "split_chunks",
        "clean_options",
    ]

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as cx:
            # v4 greenfield：清理掉所有遗留表
            cx.execute("DROP TABLE IF EXISTS audio_records")
            cx.executescript(self.SCHEMA)
            # 兼容旧库（v3 之前没有 date_str 列 / v6 加 clean_options）：ALTER TABLE 增量补列
            cols = {row[1] for row in cx.execute("PRAGMA table_info(tasks)").fetchall()}
            if "date_str" not in cols:
                cx.execute("ALTER TABLE tasks ADD COLUMN date_str TEXT NOT NULL DEFAULT ''")
            if "clean_options" not in cols:
                cx.execute("ALTER TABLE tasks ADD COLUMN clean_options TEXT")
        logger.info("TaskStore using database: %s", self._db)

    # -- connection ---------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self._db), check_same_thread=False)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    # -- writes -------------------------------------------------------------

    def insert(self, record: TaskRecord) -> None:
        placeholders = ", ".join(["?"] * len(self._ALL_COLUMNS))
        col_list = ", ".join(self._ALL_COLUMNS)
        with self._connect() as cx:
            cx.execute(
                f"INSERT INTO tasks ({col_list}) VALUES ({placeholders})",
                [getattr(record, c) for c in self._ALL_COLUMNS],
            )
        logger.debug("TaskStore inserted %s", record.task_id)

    def update_progress(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        current_stage: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
        retry_count: Optional[int] = None,
        clear_error: bool = False,
        local_clean_text: Optional[str] = None,
        normalized_text: Optional[str] = None,
        split_prompt: Optional[str] = None,
        split_chunks: Optional[str] = None,
        clear_split_chunks: bool = False,
        clean_options: Optional[str] = None,
        clear_clean_options: bool = False,
    ) -> bool:
        """部分更新任务字段。"""
        sets: list[str] = []
        params: list[object] = []
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if current_stage is not None:
            sets.append("current_stage = ?"); params.append(current_stage)
        if progress is not None:
            sets.append("progress = ?"); params.append(progress)
        if message is not None:
            sets.append("message = ?"); params.append(message)
        if error is not None:
            sets.append("error = ?"); params.append(error)
        if clear_error:
            sets.append("error = NULL")
        if retry_count is not None:
            sets.append("retry_count = ?"); params.append(retry_count)
        if local_clean_text is not None:
            sets.append("local_clean_text = ?"); params.append(local_clean_text)
        if normalized_text is not None:
            sets.append("normalized_text = ?"); params.append(normalized_text)
        if split_prompt is not None:
            sets.append("split_prompt = ?"); params.append(split_prompt)
        if split_chunks is not None:
            sets.append("split_chunks = ?"); params.append(split_chunks)
        if clear_split_chunks:
            sets.append("split_chunks = NULL")
            sets.append("split_prompt = NULL")
        if clean_options is not None:
            sets.append("clean_options = ?"); params.append(clean_options)
        if clear_clean_options:
            sets.append("clean_options = NULL")
        if not sets:
            return False
        from datetime import datetime as _dt, timezone as _tz
        sets.append("updated_at = ?")
        params.append(_dt.now(_tz.utc).isoformat().replace("+00:00", "Z"))
        params.append(task_id)
        with self._connect() as cx:
            cursor = cx.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?",
                params,
            )
            return cursor.rowcount > 0

    # -- reads --------------------------------------------------------------

    _SELECT_COLUMNS = (
        "task_id, filename, voice_id, status, current_stage, "
        "progress, message, date_str, error, "
        "created_at, updated_at, retry_count, provider, "
        "original_md_path, local_clean_text, normalized_text, split_prompt, split_chunks, "
        "clean_options"
    )

    def list_page(self, page: int, size: int) -> Tuple[List[TaskRecord], int]:
        """分页列出全部任务（任意状态），按 created_at 降序。"""
        if page < 1: page = 1
        if size < 1: size = 1
        offset = (page - 1) * size
        with self._connect() as cx:
            total = cx.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            rows = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks "
                "ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?",
                (size, offset),
            ).fetchall()
        return [self._row_to_task(r) for r in rows], total

    def list_done(self, page: int, size: int) -> Tuple[List[TaskRecord], int]:
        """分页列出 status='done' 的任务（听文档列表来源）。"""
        if page < 1: page = 1
        if size < 1: size = 1
        offset = (page - 1) * size
        with self._connect() as cx:
            total = cx.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'done'"
            ).fetchone()[0]
            rows = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks "
                "WHERE status = 'done' "
                "ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?",
                (size, offset),
            ).fetchall()
        return [self._row_to_task(r) for r in rows], total

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._connect() as cx:
            row = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def delete(self, task_id: str) -> bool:
        with self._connect() as cx:
            cursor = cx.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            return cursor.rowcount > 0

    # -- Watchdog helpers ---------------------------------------------------

    def list_processing(self) -> List[TaskRecord]:
        """列出 status='converting' 的任务（watchdog 用）。"""
        with self._connect() as cx:
            rows = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks WHERE status = 'converting'"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def mark_stalled(
        self, task_id: str, *,
        current_stage: Optional[str],
        stall_seconds: float,
        threshold_sec: float,
    ) -> bool:
        msg = (
            f"stage={current_stage or 'unknown'} 已卡住 {stall_seconds:.0f}s，"
            f"超过阈值 {threshold_sec:.0f}s，自动标记失败"
        )
        return self.update_progress(
            task_id,
            status=TASK_STATUS_FAILED_RETRYABLE,
            message=f"处理失败（可重试）：{msg}",
            error=f"stalled at stage={current_stage!r} for {stall_seconds:.0f}s",
        )

    def mark_subtitle_pending(
        self, task_id: str, *,
        date_str: str, subtitle_error: str,
    ) -> bool:
        """把任务置为 subtitle_pending（音频已成功，字幕待重试）。

        调用方负责保证任务目录里 ``<task_id>.mp3`` 已存在；library 由听文档
        列表自动从 tasks.status='done'|其他 推断。
        """
        return self.update_progress(
            task_id,
            status=TASK_STATUS_SUBTITLE_PENDING,
            current_stage="subtitle_pending",
            progress=0.95,
            message=f"音频已生成（{task_id[:8]}…），字幕待重试：{subtitle_error[:200]}",
            error=f"subtitle_pending: {subtitle_error}",
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        def _safe(col: str, default=None):
            try:
                return row[col]
            except (KeyError, IndexError):
                return default

        return TaskRecord(
            task_id=row["task_id"],
            filename=row["filename"],
            voice_id=row["voice_id"],
            status=row["status"],
            current_stage=row["current_stage"],
            progress=row["progress"],
            message=row["message"],
            date_str=row["date_str"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            retry_count=_safe("retry_count", 0),
            provider=_safe("provider"),
            original_md_path=_safe("original_md_path"),
            local_clean_text=_safe("local_clean_text"),
            normalized_text=_safe("normalized_text"),
            split_prompt=_safe("split_prompt"),
            split_chunks=_safe("split_chunks"),
            clean_options=_safe("clean_options"),
        )


# ---- Tasks: 状态常量（跨模块共享）-----------------------------------------


# 新状态机（分步交互流程）
TASK_STATUS_DRAFT = "draft"
TASK_STATUS_NORMALIZING = "normalizing"
TASK_STATUS_NORMALIZED = "normalized"
TASK_STATUS_READY_TO_SPLIT = "ready_to_split"
TASK_STATUS_SPLITTING = "splitting"
TASK_STATUS_SPLITTED = "splitted"
# v6：splitted 与 ready_to_convert 之间新增"本地清洗"过渡
TASK_STATUS_LOCAL_CLEANING = "local_cleaning"
TASK_STATUS_LOCAL_CLEANED = "local_cleaned"
TASK_STATUS_READY_TO_CONVERT = "ready_to_convert"
TASK_STATUS_CONVERTING = "converting"
# 字幕待重试：convert 阶段音频已成功，但 minimax subtitle_file 拉取失败。
# 任务状态置为 subtitle_pending，音频仍可听（<task_id>.mp3 已落盘），用户点重试后
# 只重试字幕拉取。watchdog 跳过此状态（非真正的"卡死"，是等用户操作）。
TASK_STATUS_SUBTITLE_PENDING = "subtitle_pending"
TASK_STATUS_DONE = "done"
TASK_STATUS_ERROR = "error"
TASK_STATUS_FAILED_RETRYABLE = "failed_retryable"

# 旧状态机（保留 re-export 兼容旧测试，但不再用于新流程）
TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"

# 可重试的终端状态（error / failed_retryable / subtitle_pending 都可点重试）
TASK_RETRYABLE_STATUSES = (
    TASK_STATUS_ERROR,
    TASK_STATUS_FAILED_RETRYABLE,
    TASK_STATUS_SUBTITLE_PENDING,
)
# 需要 watchdog 监控的运行中状态（subtitle_pending 是等用户操作，watchdog 跳过）
TASK_RUNNING_STATUSES = (
    TASK_STATUS_NORMALIZING,
    TASK_STATUS_SPLITTING,
    TASK_STATUS_LOCAL_CLEANING,
    TASK_STATUS_CONVERTING,
)


# ---- Settings: 运行时可切换的应用设置 ---------------------------------------


class SettingsStore:
    """运行时可切换的应用设置（持久化到 SQLite app_settings 表）。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS app_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT
    );
    """

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as cx:
            cx.executescript(self.SCHEMA)
        logger.info("SettingsStore using database: %s", self._db)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self._db), check_same_thread=False)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as cx:
            row = cx.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as cx:
            cx.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now),
            )

    def all(self) -> dict:
        with self._connect() as cx:
            rows = cx.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}