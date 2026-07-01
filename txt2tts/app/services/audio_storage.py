"""Audio file persistence + serving.

Each generated mp3 is stored under  outputs/<YYYY-MM-DD>/<uuid>.mp3 .
The router exposes  GET /api/audio/{audio_id}  which streams the file back
with HTTP Range support (FastAPI's FileResponse handles it automatically).
"""
from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

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