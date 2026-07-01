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
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredAudio:
    audio_id: str
    file_path: Path


class AudioStorageService:
    """Owns the on-disk layout for generated audio files."""

    def __init__(self, output_dir: Path) -> None:
        self._root = Path(output_dir).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        logger.info("AudioStorageService using directory: %s", self._root)

    # -- write side ----------------------------------------------------------

    def save(self, audio_bytes: bytes, subdir: Optional[str] = None) -> StoredAudio:
        """Persist raw audio bytes and return the storage handle."""
        if not audio_bytes:
            raise ValueError("Refusing to store empty audio payload.")

        day = subdir or datetime.now().strftime("%Y-%m-%d")
        day_dir = self._root / day
        day_dir.mkdir(parents=True, exist_ok=True)

        audio_id = uuid.uuid4().hex
        file_path = day_dir / f"{audio_id}.mp3"
        file_path.write_bytes(audio_bytes)
        logger.info("Stored audio %s (%d bytes) at %s", audio_id, len(audio_bytes), file_path)
        return StoredAudio(audio_id=audio_id, file_path=file_path)

    # -- read side -----------------------------------------------------------

    def resolve(self, audio_id: str) -> Optional[Path]:
        """Find a stored audio by id, scanning date subdirs. Returns None if missing."""
        # Try today first, then walk.
        today = self._root / datetime.now().strftime("%Y-%m-%d") / f"{audio_id}.mp3"
        if today.exists():
            return today
        for candidate in self._root.rglob(f"{audio_id}.mp3"):
            if candidate.is_file():
                return candidate
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
        created_at        TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audio_records_created_at
        ON audio_records(created_at DESC);
    """

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as cx:
            cx.executescript(self.SCHEMA)
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
                    voice_id, duration_sec, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        logger.debug("LibraryStore inserted %s", record.audio_id)

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
                       voice_id, duration_sec, byte_size, created_at
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
                       voice_id, duration_sec, byte_size, created_at
                FROM audio_records
                WHERE audio_id = ?
                """,
                (audio_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AudioRecord:
        return AudioRecord(
            audio_id=row["audio_id"],
            original_filename=row["original_filename"],
            original_md=row["original_md"],
            normalized_md=row["normalized_md"],
            voice_id=row["voice_id"],
            duration_sec=row["duration_sec"],
            byte_size=row["byte_size"],
            created_at=row["created_at"],
        )