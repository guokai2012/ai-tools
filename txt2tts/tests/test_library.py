"""Unit tests for the SQLite-backed LibraryStore (听文档 index)."""
from pathlib import Path

from app.services.audio_storage import AudioRecord, LibraryStore


def _record(audio_id: str, *, created_at: str = "2026-07-01T08:00:00Z",
            filename: str = "demo.md", byte_size: int = 1024) -> AudioRecord:
    return AudioRecord(
        audio_id=audio_id,
        original_filename=filename,
        original_md="# title\n\nbody",
        normalized_md="title\n\nbody",
        voice_id="冰糖",
        duration_sec=None,
        byte_size=byte_size,
        created_at=created_at,
    )


def test_library_empty_list(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    items, total = store.list_page(page=1, size=10)
    assert items == []
    assert total == 0


def test_library_insert_and_list(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    store.insert(_record("aaa"))
    store.insert(_record("bbb", filename="b.md", byte_size=2048))

    items, total = store.list_page(page=1, size=10)
    assert total == 2
    # Newest first.
    assert items[0].audio_id == "bbb"
    assert items[0].original_filename == "b.md"
    assert items[0].byte_size == 2048
    assert items[1].audio_id == "aaa"


def test_library_pagination(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    # Insert with strictly increasing created_at so order is deterministic.
    for i in range(5):
        store.insert(_record(f"id{i}", created_at=f"2026-07-0{i + 1}T00:00:00Z"))

    page1, total1 = store.list_page(page=1, size=2)
    page2, total2 = store.list_page(page=2, size=2)
    page3, total3 = store.list_page(page=3, size=2)

    assert total1 == total2 == total3 == 5
    assert [r.audio_id for r in page1] == ["id4", "id3"]
    assert [r.audio_id for r in page2] == ["id2", "id1"]
    assert [r.audio_id for r in page3] == ["id0"]


def test_library_get_existing(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    store.insert(_record("xyz", filename="hello.md"))
    rec = store.get("xyz")
    assert rec is not None
    assert rec.original_filename == "hello.md"
    assert rec.normalized_md == "title\n\nbody"
    assert rec.voice_id == "冰糖"


def test_library_get_missing(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    assert store.get("does-not-exist") is None


def test_library_upsert_overwrites(tmp_path: Path):
    store = LibraryStore(tmp_path / "lib.db")
    store.insert(_record("dup", filename="a.md"))
    store.insert(_record("dup", filename="b.md"))
    items, total = store.list_page(page=1, size=10)
    assert total == 1
    assert items[0].original_filename == "b.md"


def test_library_persists_across_instances(tmp_path: Path):
    """Reopening the same DB file must preserve the data."""
    db = tmp_path / "lib.db"
    s1 = LibraryStore(db)
    s1.insert(_record("persist"))

    s2 = LibraryStore(db)
    items, total = s2.list_page(page=1, size=10)
    assert total == 1
    assert items[0].audio_id == "persist"