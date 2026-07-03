"""Audio file persistence + serving.

Each generated mp3 is stored under  outputs/<YYYY-MM-DD>/<uuid>.mp3 .
The router exposes  GET /api/audio/{audio_id}  which streams the file back
with HTTP Range support (FastAPI's FileResponse handles it automatically).

For the 「听文档」 (library) feature we also persist a metadata index in
SQLite via :class:`LibraryStore`, which maps each ``audio_id`` back to the
original Markdown text and the M3-normalized text used for TTS.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredAudio:
    audio_id: str
    file_path: Path


class AudioStorageService:
    """Owns the on-disk layout for generated audio files.

    目录布局（自 v2 起）：
        ``<output_dir>/audio/<audio_id>.mp3``         最终成品（成功任务的播放入口）
        ``<output_dir>/audio/_artifacts/<audio_id>/`` 中间产物快照（normalized.md / 子段 mp3 / srt / lrc / 原始 md）
        ``<output_dir>/uploads/<task_id>.md``         原始 md，仅用于进行中 / 失败任务的重试；成功后会被 promote 到 ``_artifacts/<audio_id>/``
        ``<output_dir>/chunks/<task_id>/...``         mimo provider 流水线中段产物，成功后被 promote
        ``<output_dir>/segments/<task_id>/...``       edge provider 流水线中段产物，成功后被 promote

    旧的 ``<output_dir>/<YYYY-MM-DD>/<audio_id>.mp3`` 仍被 ``resolve()`` 兜底识别，保证历史数据可访问。
    """

    # 子目录名常量（多处复用，避免散落字面量）
    AUDIO_DIR = "audio"
    ARTIFACTS_SUBDIR = "_artifacts"
    UPLOADS_SUBDIR = "uploads"
    CHUNKS_SUBDIR = "chunks"
    SEGMENTS_SUBDIR = "segments"

    def __init__(self, output_dir: Path) -> None:
        self._root = Path(output_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / self.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
        logger.info("AudioStorageService using directory: %s", self._root)

    # -- write side ----------------------------------------------------------

    @property
    def audio_dir(self) -> Path:
        return self._root / self.AUDIO_DIR

    def artifacts_dir(self, audio_id: str) -> Path:
        return self.audio_dir / self.ARTIFACTS_SUBDIR / audio_id

    def save(self, audio_bytes: bytes, subdir: Optional[str] = None) -> StoredAudio:
        """Persist raw audio bytes and return the storage handle.

        v2 起：成品统一写到 ``audio/<audio_id>.mp3``，不再按日期散落。
        ``subdir`` 参数为兼容旧调用保留：仍接受，但忽略日期目录创建。
        """
        if not audio_bytes:
            raise ValueError("Refusing to store empty audio payload.")

        self.audio_dir.mkdir(parents=True, exist_ok=True)
        audio_id = uuid.uuid4().hex
        file_path = self.audio_dir / f"{audio_id}.mp3"
        file_path.write_bytes(audio_bytes)
        logger.info("Stored audio %s (%d bytes) at %s", audio_id, len(audio_bytes), file_path)
        return StoredAudio(audio_id=audio_id, file_path=file_path)

    # -- read side -----------------------------------------------------------

    def resolve(self, audio_id: str) -> Optional[Path]:
        """Find a stored audio by id.

        查找顺序：
            1. ``audio/<audio_id>.mp3``（新约定）
            2. ``<YYYY-MM-DD>/<audio_id>.mp3``（今天 + 历史日期，兼容旧数据）
            3. 全目录 rglob 兜底
        """
        # 1. 新约定
        new_path = self.audio_dir / f"{audio_id}.mp3"
        if new_path.exists():
            return new_path
        # 2. 旧约定（按日期）
        today = self._root / datetime.now().strftime("%Y-%m-%d") / f"{audio_id}.mp3"
        if today.exists():
            return today
        # 3. rglob 兜底（任何子目录）
        for candidate in self._root.rglob(f"{audio_id}.mp3"):
            if candidate.is_file():
                return candidate
        return None

    def resolve_lyrics(self, audio_id: str) -> Optional[Path]:
        """找指定 audio_id 的 LRC 文件；优先 ``audio/_artifacts/<audio_id>/`` 新位置。"""
        new_loc = self.artifacts_dir(audio_id) / f"{audio_id}.lrc"
        if new_loc.exists():
            return new_loc
        # 兼容旧版 lyrics/<audio_id>.lrc 路径
        legacy = self._root / "lyrics" / f"{audio_id}.lrc"
        if legacy.exists():
            return legacy
        return None

    def stats(self) -> dict:
        total = sum(p.stat().st_size for p in self._root.rglob("*.mp3") if p.is_file())
        count = sum(1 for _ in self._root.rglob("*.mp3") if _.is_file())
        return {"directory": str(self._root), "count": count, "total_bytes": total}

    def cleanup_older_than_days(self, days: int) -> int:
        """Delete mp3s older than `days`. Returns number removed."""
        if days <= 0:
            return 0
        cutoff = datetime.now().timestamp() - days * 86400
        removed = 0
        for p in self._root.rglob("*.mp3"):
            if not p.is_file():
                continue
            if p.stat().st_mtime < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # -- task lifecycle: promote & delete ------------------------------------

    def promote_artifacts(
        self,
        *,
        task_id: str,
        audio_id: str,
    ) -> dict:
        """任务成功后，把 chunks / segments / uploads.md 全部移到 ``audio/_artifacts/<audio_id>/``。

        完成后 ``uploads/<task_id>.md`` 不再保留（避免被 watchdog 误判为新任务原料）；
        听文档 / 后续下载通过 ``audio/<audio_id>.mp3`` + ``_artifacts/<audio_id>/`` 访问。

        返回被移动的路径数；任何路径不存在都不会抛错。
        """
        import shutil
        dest = self.artifacts_dir(audio_id)
        dest.mkdir(parents=True, exist_ok=True)

        moved = 0

        def _move(src: Path) -> None:
            nonlocal moved
            if not src.exists():
                return
            target = dest / src.name
            if target.exists():
                # 极端情况：同一 audio_id 被重用？用临时后缀避免覆盖
                stem, suffix = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = dest / f"{stem}.{i}{suffix}"
                    i += 1
            shutil.move(str(src), str(target))
            moved += 1
            logger.info("promote_artifacts: %s -> %s", src, target)

        # 1) chunks/<task_id>/ 整个目录
        chunks_root = self._root / self.CHUNKS_SUBDIR / task_id
        if chunks_root.is_dir():
            # 把 chunks 目录里的所有文件直接移过去（保留命名）
            for entry in list(chunks_root.iterdir()):
                _move(entry)
            try:
                chunks_root.rmdir()
            except OSError:
                logger.warning("promote_artifacts: failed to rmdir %s", chunks_root)

        # 2) segments/<task_id>/ 整个目录（edge）
        seg_root = self._root / self.SEGMENTS_SUBDIR / task_id
        if seg_root.is_dir():
            for entry in list(seg_root.iterdir()):
                _move(entry)
            try:
                seg_root.rmdir()
            except OSError:
                logger.warning("promote_artifacts: failed to rmdir %s", seg_root)

        # 3) uploads/<task_id>.md → 重命名为 <audio_id>.md 保留原文
        upload = self._root / self.UPLOADS_SUBDIR / f"{task_id}.md"
        if upload.exists():
            target = dest / f"{audio_id}.md"
            if target.exists():
                # 已存在同名（重试场景），覆盖
                try:
                    target.unlink()
                except OSError:
                    pass
            shutil.move(str(upload), str(target))
            moved += 1
            logger.info("promote_artifacts: upload moved to %s", target)

        # 4) 兼容老路径：<YYYY-MM-DD>/<audio_id>.mp3 如果存在，移到 audio/ 中心目录
        # 这样 DELETE 之前 GET /api/audio/{id} 一定能命中新约定
        # 不强制：仅在 audio_dir 里没有同名 mp3 时才搬，避免重复
        final_in_audio = self.audio_dir / f"{audio_id}.mp3"
        if not final_in_audio.exists():
            for candidate in self._root.rglob(f"{audio_id}.mp3"):
                if candidate.is_file() and candidate != final_in_audio:
                    try:
                        shutil.move(str(candidate), str(final_in_audio))
                        moved += 1
                        logger.info(
                            "promote_artifacts: legacy mp3 %s -> %s",
                            candidate, final_in_audio,
                        )
                    except OSError as exc:
                        logger.warning(
                            "promote_artifacts: failed to move %s: %s", candidate, exc,
                        )
                    break  # 只搬一份（mp3 是单一文件）

        logger.info(
            "promote_artifacts done: audio_id=%s, moved=%d entries", audio_id, moved,
        )
        return {"audio_id": audio_id, "task_id": task_id, "moved": moved}

    def delete_task_files(
        self,
        *,
        task_id: str,
        audio_id: Optional[str] = None,
        keep_final_audio: bool = False,
    ) -> dict:
        """删除一个任务的所有派生文件（不含 tasks 表行，那由 TaskStore.delete 处理）。

        参数:
            task_id: 任务 id（必删 uploads/<task_id>.md / chunks/<task_id>/ / segments/<task_id>/）
            audio_id: 可选；任务的最终 audio_id（如果任务曾成功过）
            keep_final_audio: True 时保留 ``audio/<audio_id>.mp3`` 与 ``_artifacts/<audio_id>/``；
                              False 时连同它们一起删（彻底清空）。

        返回删除摘要（每个类别的文件数）。
        """
        import shutil
        removed = {
            "uploads": 0,
            "chunks": 0,
            "segments": 0,
            "final_mp3": 0,
            "artifacts": 0,
        }

        # uploads/<task_id>.md
        upload = self._root / self.UPLOADS_SUBDIR / f"{task_id}.md"
        if upload.exists():
            try:
                upload.unlink()
                removed["uploads"] += 1
            except OSError as exc:
                logger.warning("delete_task_files: failed to unlink %s: %s", upload, exc)

        # chunks/<task_id>/
        chunks_root = self._root / self.CHUNKS_SUBDIR / task_id
        if chunks_root.exists():
            try:
                shutil.rmtree(chunks_root)
                removed["chunks"] += 1
            except OSError as exc:
                logger.warning("delete_task_files: failed to rmtree %s: %s", chunks_root, exc)

        # segments/<task_id>/
        seg_root = self._root / self.SEGMENTS_SUBDIR / task_id
        if seg_root.exists():
            try:
                shutil.rmtree(seg_root)
                removed["segments"] += 1
            except OSError as exc:
                logger.warning("delete_task_files: failed to rmtree %s: %s", seg_root, exc)

        # final mp3 + artifacts
        if audio_id:
            if keep_final_audio:
                # 保留 audio/<audio_id>.mp3（任务成功的兜底播放文件）
                # 同时保留 _artifacts/<audio_id>/（中间产物 + 原始 md）
                logger.info(
                    "delete_task_files: keep_final_audio=True, "
                    "preserving audio/%s.mp3 and _artifacts/%s/",
                    audio_id, audio_id,
                )
            else:
                # 删 audio/<audio_id>.mp3
                final = self.audio_dir / f"{audio_id}.mp3"
                if final.exists():
                    try:
                        final.unlink()
                        removed["final_mp3"] += 1
                    except OSError as exc:
                        logger.warning("delete_task_files: failed to unlink %s: %s", final, exc)
                # 兼容旧位置残留：<YYYY-MM-DD>/<audio_id>.mp3
                for legacy in self._root.rglob(f"{audio_id}.mp3"):
                    if legacy.is_file() and legacy != final:
                        try:
                            legacy.unlink()
                            removed["final_mp3"] += 1
                        except OSError:
                            pass
                # 删 _artifacts/<audio_id>/
                art = self.artifacts_dir(audio_id)
                if art.exists():
                    try:
                        shutil.rmtree(art)
                        removed["artifacts"] += 1
                    except OSError as exc:
                        logger.warning("delete_task_files: failed to rmtree %s: %s", art, exc)
                # 删 lyrics/<audio_id>.lrc 旧位置
                legacy_lyrics = self._root / "lyrics" / f"{audio_id}.lrc"
                if legacy_lyrics.exists():
                    try:
                        legacy_lyrics.unlink()
                    except OSError:
                        pass

        logger.info(
            "delete_task_files done: task_id=%s audio_id=%s keep_final=%s removed=%s",
            task_id, audio_id, keep_final_audio, removed,
        )
        return removed


# ---- Library: SQLite-backed metadata index -------------------------------


@dataclass(frozen=True)
class AudioRecord:
    """One row in the ``audio_records`` table — enough to render a library
    entry and a player-detail page without re-reading the original .md file."""

    audio_id: str
    original_filename: str
    original_md: str
    normalized_md: str
    voice_id: Optional[str]
    duration_sec: Optional[float]
    byte_size: int
    created_at: str  # ISO-8601, e.g. "2026-07-01T08:30:00Z"
    lyrics_path: Optional[str] = None  # 相对 output_dir 的歌词文件路径
    provider: Optional[str] = None     # "mimo" | "edge"，前端徽章用


class LibraryStore:
    """SQLite-backed metadata index for generated audios.

    The DB lives at ``<output_dir>/library.db`` by convention. Lookups use
    ``audio_id`` as the primary key; pagination sorts by ``created_at``
    descending so the freshest items surface first.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS audio_records (
        audio_id          TEXT PRIMARY KEY,
        original_filename TEXT NOT NULL,
        original_md       TEXT NOT NULL,
        normalized_md     TEXT NOT NULL,
        voice_id          TEXT,
        duration_sec      REAL,
        byte_size         INTEGER NOT NULL,
        created_at        TEXT NOT NULL,
        lyrics_path       TEXT,
        provider          TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_audio_records_created_at
        ON audio_records(created_at DESC);
    """

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as cx:
            cx.executescript(self.SCHEMA)
            # 向后兼容：旧库没有 lyrics_path / provider 列时加一列。
            cols = {row[1] for row in cx.execute("PRAGMA table_info(audio_records)").fetchall()}
            if "lyrics_path" not in cols:
                cx.execute("ALTER TABLE audio_records ADD COLUMN lyrics_path TEXT")
            if "provider" not in cols:
                cx.execute("ALTER TABLE audio_records ADD COLUMN provider TEXT")
        logger.info("LibraryStore using database: %s", self._db)

    # -- low-level connection ------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self._db), check_same_thread=False)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    # -- writes --------------------------------------------------------------

    def insert(self, record: AudioRecord) -> None:
        """Upsert a record keyed by ``audio_id``."""
        with self._connect() as cx:
            cx.execute(
                """
                INSERT OR REPLACE INTO audio_records (
                    audio_id, original_filename, original_md, normalized_md,
                    voice_id, duration_sec, byte_size, created_at, lyrics_path, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.audio_id,
                    record.original_filename,
                    record.original_md,
                    record.normalized_md,
                    record.voice_id,
                    record.duration_sec,
                    record.byte_size,
                    record.created_at,
                    record.lyrics_path,
                    record.provider,
                ),
            )
        logger.debug("LibraryStore inserted %s", record.audio_id)

    def attach_lyrics(self, audio_id: str, lyrics_path: str) -> bool:
        """回填歌词文件路径（相对 output_dir）。"""
        with self._connect() as cx:
            cursor = cx.execute(
                "UPDATE audio_records SET lyrics_path = ? WHERE audio_id = ?",
                (lyrics_path, audio_id),
            )
            return cursor.rowcount > 0

    # -- reads ---------------------------------------------------------------

    def list_page(self, page: int, size: int) -> Tuple[List[AudioRecord], int]:
        """Return ``(items, total)`` for the requested 1-indexed page."""
        if page < 1:
            page = 1
        if size < 1:
            size = 1
        offset = (page - 1) * size
        with self._connect() as cx:
            total = cx.execute("SELECT COUNT(*) FROM audio_records").fetchone()[0]
            rows = cx.execute(
                """
                SELECT audio_id, original_filename, original_md, normalized_md,
                       voice_id, duration_sec, byte_size, created_at, lyrics_path, provider
                FROM audio_records
                ORDER BY created_at DESC, audio_id DESC
                LIMIT ? OFFSET ?
                """,
                (size, offset),
            ).fetchall()
        return [self._row_to_record(r) for r in rows], total

    def get(self, audio_id: str) -> Optional[AudioRecord]:
        """Fetch a single record by id, or None if missing."""
        with self._connect() as cx:
            row = cx.execute(
                """
                SELECT audio_id, original_filename, original_md, normalized_md,
                       voice_id, duration_sec, byte_size, created_at, lyrics_path, provider
                FROM audio_records
                WHERE audio_id = ?
                """,
                (audio_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def delete(self, audio_id: str) -> bool:
        """按 audio_id 删除一条 audio_records 行。返回是否找到并删除。"""
        with self._connect() as cx:
            cursor = cx.execute(
                "DELETE FROM audio_records WHERE audio_id = ?",
                (audio_id,),
            )
            return cursor.rowcount > 0

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AudioRecord:
        # 新增列在旧库可能不存在（try/except 容错）
        try:
            lyrics_path = row["lyrics_path"]
        except (KeyError, IndexError):
            lyrics_path = None
        try:
            provider = row["provider"]
        except (KeyError, IndexError):
            provider = None
        return AudioRecord(
            audio_id=row["audio_id"],
            original_filename=row["original_filename"],
            original_md=row["original_md"],
            normalized_md=row["normalized_md"],
            voice_id=row["voice_id"],
            duration_sec=row["duration_sec"],
            byte_size=row["byte_size"],
            created_at=row["created_at"],
            lyrics_path=lyrics_path,
            provider=provider,
        )


# ---- Task: 后台转语音任务索引 ---------------------------------------------


@dataclass(frozen=True)
class TaskRecord:
    """``tasks`` 表的一行 —— 描述一个后台 TTS 转换任务的生命周期。"""

    task_id: str
    filename: str
    voice_id: Optional[str]
    status: str          # 新状态机：draft/normalizing/normalized/ready_to_split/splitting/splitted/ready_to_convert/converting/done/error/failed_retryable
    current_stage: Optional[str]
    progress: float     # 0.0 .. 1.0
    message: str
    audio_id: Optional[str]   # 成功后写入
    error: Optional[str]      # 失败后写入
    created_at: str
    updated_at: str
    # 原始 md 落盘路径（相对 output_dir），用于重试时重新读取
    original_md_path: Optional[str] = None
    # 重试次数（初始 0，每次重试 +1）
    retry_count: int = 0
    # 任务使用的 TTS 方案：mimo / edge。前端据此决定任务详情页展示的步骤条
    provider: Optional[str] = None
    # ---- 新增：分步交互流程字段 ----
    # 本地 Markdown 清洗结果（上传时写入）
    local_clean_text: Optional[str] = None
    # M3 标准化结果（标准化后写入；跳过标准化时 = local_clean_text）
    normalized_text: Optional[str] = None
    # 用户选的拆分提示词（最后使用的那条）
    split_prompt: Optional[str] = None
    # M3 拆分后的子文档列表（JSON 数组字符串）
    split_chunks: Optional[str] = None


class TaskStore:
    """SQLite-backed 后台任务索引，与 LibraryStore 共用同一个 ``library.db``。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id           TEXT PRIMARY KEY,
        filename          TEXT NOT NULL,
        voice_id          TEXT,
        status            TEXT NOT NULL DEFAULT 'draft',
        current_stage     TEXT,
        progress          REAL NOT NULL DEFAULT 0.0,
        message           TEXT NOT NULL DEFAULT '',
        audio_id          TEXT,
        error             TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        original_md_path  TEXT,
        retry_count       INTEGER NOT NULL DEFAULT 0,
        provider          TEXT,
        local_clean_text  TEXT,
        normalized_text    TEXT,
        split_prompt      TEXT,
        split_chunks      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_created_at
        ON tasks(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_tasks_status
        ON tasks(status);
    """

    # 所有列名（insert / select / 迁移统一用）
    _ALL_COLUMNS = [
        "task_id", "filename", "voice_id", "status", "current_stage",
        "progress", "message", "audio_id", "error",
        "created_at", "updated_at", "original_md_path", "retry_count", "provider",
        "local_clean_text", "normalized_text", "split_prompt", "split_chunks",
    ]

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as cx:
            cx.executescript(self.SCHEMA)
            # 迁移：旧库补列
            cols = {row[1] for row in cx.execute("PRAGMA table_info(tasks)").fetchall()}
            migrate_cols = {
                "original_md_path": "TEXT",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "provider": "TEXT",
                "local_clean_text": "TEXT",
                "normalized_text": "TEXT",
                "split_prompt": "TEXT",
                "split_chunks": "TEXT",
            }
            for col_name, col_type in migrate_cols.items():
                if col_name not in cols:
                    cx.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
            # 清除旧状态数据（状态机变更，旧 pending/processing 任务不再兼容）
            cx.execute("DELETE FROM tasks")
            cx.execute("DELETE FROM audio_records")
            logger.info("TaskStore: cleared old task/audio_records data (status machine changed)")
        logger.info("TaskStore using database: %s", self._db)

    # -- low-level connection ------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self._db), check_same_thread=False)
        cx.row_factory = sqlite3.Row
        try:
            yield cx
            cx.commit()
        finally:
            cx.close()

    # -- writes --------------------------------------------------------------

    def insert(self, record: TaskRecord) -> None:
        """插入一条新任务记录（不支持 upsert，task_id 天然唯一）。"""
        col_names = self._ALL_COLUMNS
        placeholders = ", ".join(["?"] * len(col_names))
        col_list = ", ".join(col_names)
        with self._connect() as cx:
            cx.execute(
                f"INSERT INTO tasks ({col_list}) VALUES ({placeholders})",
                (
                    record.task_id,
                    record.filename,
                    record.voice_id,
                    record.status,
                    record.current_stage,
                    record.progress,
                    record.message,
                    record.audio_id,
                    record.error,
                    record.created_at,
                    record.updated_at,
                    record.original_md_path,
                    record.retry_count,
                    record.provider,
                    record.local_clean_text,
                    record.normalized_text,
                    record.split_prompt,
                    record.split_chunks,
                ),
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
        audio_id: Optional[str] = None,
        error: Optional[str] = None,
        retry_count: Optional[int] = None,
        clear_error: bool = False,
        local_clean_text: Optional[str] = None,
        normalized_text: Optional[str] = None,
        split_prompt: Optional[str] = None,
        split_chunks: Optional[str] = None,
    ) -> bool:
        """部分更新任务字段（只更新传入的非 None 字段）。返回是否找到记录。

        clear_error=True 时显式把 error 列设为 NULL（重试时清空旧错误）。
        """
        sets: list[str] = []
        params: list[object] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if current_stage is not None:
            sets.append("current_stage = ?")
            params.append(current_stage)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if message is not None:
            sets.append("message = ?")
            params.append(message)
        if audio_id is not None:
            sets.append("audio_id = ?")
            params.append(audio_id)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if clear_error:
            sets.append("error = NULL")
        if retry_count is not None:
            sets.append("retry_count = ?")
            params.append(retry_count)
        if local_clean_text is not None:
            sets.append("local_clean_text = ?")
            params.append(local_clean_text)
        if normalized_text is not None:
            sets.append("normalized_text = ?")
            params.append(normalized_text)
        if split_prompt is not None:
            sets.append("split_prompt = ?")
            params.append(split_prompt)
        if split_chunks is not None:
            sets.append("split_chunks = ?")
            params.append(split_chunks)
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

    # -- reads ---------------------------------------------------------------

    _SELECT_COLUMNS = (
        "task_id, filename, voice_id, status, current_stage, "
        "progress, message, audio_id, error, "
        "created_at, updated_at, original_md_path, retry_count, provider, "
        "local_clean_text, normalized_text, split_prompt, split_chunks"
    )

    def list_page(self, page: int, size: int) -> Tuple[List[TaskRecord], int]:
        """返回 ``(items, total)``，按 created_at 降序。"""
        if page < 1:
            page = 1
        if size < 1:
            size = 1
        offset = (page - 1) * size
        with self._connect() as cx:
            total = cx.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            rows = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks "
                "ORDER BY created_at DESC, task_id DESC "
                "LIMIT ? OFFSET ?",
                (size, offset),
            ).fetchall()
        return [self._row_to_task(r) for r in rows], total

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 id 查询单条任务，不存在返回 None。"""
        with self._connect() as cx:
            row = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def delete(self, task_id: str) -> bool:
        """按 task_id 删除一条 tasks 行。返回是否找到并删除。

        调用方负责先删对应文件（TaskManager.delete_task 编排）。
        """
        with self._connect() as cx:
            cursor = cx.execute(
                "DELETE FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            return cursor.rowcount > 0

    # ---- Watchdog helpers -------------------------------------------------

    def list_processing(self) -> List[TaskRecord]:
        """返回所有 status='converting' 的任务（不分页），用于后台 watchdog。

        只读不写；调用方根据 updated_at 与当前时间比较判定是否卡死。
        """
        with self._connect() as cx:
            rows = cx.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM tasks "
                "WHERE status = 'converting'"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def mark_stalled(
        self,
        task_id: str,
        *,
        current_stage: Optional[str],
        stall_seconds: float,
        threshold_sec: float,
    ) -> bool:
        """把指定 task 标记为 failed_retryable，并附 stall 错误信息。

        返回是否成功找到并更新记录（False = 任务已经被并发改成 done/error）。
        """
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

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        # 旧库没有新列时 try 容错
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
            audio_id=row["audio_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            original_md_path=_safe("original_md_path"),
            retry_count=_safe("retry_count", 0),
            provider=_safe("provider"),
            local_clean_text=_safe("local_clean_text"),
            normalized_text=_safe("normalized_text"),
            split_prompt=_safe("split_prompt"),
            split_chunks=_safe("split_chunks"),
        )


# ---- Tasks: 任务状态常量（跨模块共享） --------------------------------------

# 新状态机（分步交互流程）
TASK_STATUS_DRAFT = "draft"
TASK_STATUS_NORMALIZING = "normalizing"
TASK_STATUS_NORMALIZED = "normalized"
TASK_STATUS_READY_TO_SPLIT = "ready_to_split"
TASK_STATUS_SPLITTING = "splitting"
TASK_STATUS_SPLITTED = "splitted"
TASK_STATUS_READY_TO_CONVERT = "ready_to_convert"
TASK_STATUS_CONVERTING = "converting"
TASK_STATUS_DONE = "done"
TASK_STATUS_ERROR = "error"
TASK_STATUS_FAILED_RETRYABLE = "failed_retryable"

# 旧状态机（保留 re-export 兼容旧测试，但不再用于新流程）
TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"

# 可重试的终端状态
TASK_RETRYABLE_STATUSES = (TASK_STATUS_ERROR, TASK_STATUS_FAILED_RETRYABLE)
# 需要 watchdog 监控的运行中状态
TASK_RUNNING_STATUSES = (TASK_STATUS_NORMALIZING, TASK_STATUS_SPLITTING, TASK_STATUS_CONVERTING)


# ---- Settings: 运行时可切换的应用设置 ---------------------------------------


class SettingsStore:
    """运行时可修改的应用设置（目前主要是 TTS provider）。

    与 `AppSettings`（环境变量驱动、启动时冻结）解耦，专门存放用户通过
    `POST /api/settings` 改写、跨进程持续化的开关。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS app_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
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
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as cx:
            cx.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def all(self) -> dict:
        with self._connect() as cx:
            rows = cx.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
