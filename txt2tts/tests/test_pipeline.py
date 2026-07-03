"""Unit tests for the TtsPipeline progress generator."""
import asyncio
import pytest

from app.services.audio_storage import AudioStorageService
from app.services.audio_storage import StoredAudio
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.pipeline import ProgressEvent, TtsPipeline
from app.services.tts_client import TtsApiError, TtsClient


pytestmark = pytest.mark.asyncio


class FakeMd:
    def to_plain_text(self, t: str) -> str:
        return "CLEANED:" + t


class FakeLlm:
    def __init__(self, fail: bool = False, text: str = "NORMALIZED"):
        self._fail = fail
        self._text = text
    async def normalize(self, t: str) -> str:
        if self._fail:
            raise LlmNormalizationError("boom")
        return self._text


class FakeTts:
    def __init__(self, fail: bool = False, audio: bytes = None):
        from types import SimpleNamespace
        self._fail = fail
        # 默认返回真实 mp3（0.3s 静音）以保证 ffmpeg concat 成功
        self._audio = audio if audio is not None else _make_real_mp3(0.3)
        self._settings = SimpleNamespace(max_input_chars_per_request=4500)
    async def synthesize(self, text: str, voice=None) -> bytes:
        if self._fail:
            raise TtsApiError("tts boom")
        return self._audio


def _make_real_mp3(duration_sec: float = 0.3) -> bytes:
    """生成一个真实 mp3 字节，供 ffmpeg concat 测试用。"""
    import subprocess
    from pathlib import Path
    ffmpeg = Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        return b"FAKEAUDIO"  # fallback
    out = subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={duration_sec}",
         "-ar", "22050", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "64k",
         "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    )
    return out.stdout


class FakeAudio:
    def __init__(self, root=None):
        from pathlib import Path
        import tempfile
        self._root = Path(root) if root else Path(tempfile.gettempdir()) / "txt2tts_fake_audio"
        self._root.mkdir(parents=True, exist_ok=True)
    def save(self, data: bytes) -> StoredAudio:
        return StoredAudio(audio_id="abc123", file_path=None)


def _pipeline(md=None, llm=None, tts=None, audio=None, ffmpeg_path=None, ffprobe_path=None) -> TtsPipeline:
    from pathlib import Path
    return TtsPipeline(
        md or FakeMd(),
        llm or FakeLlm(),
        tts or FakeTts(),
        audio or FakeAudio(),
        ffmpeg_path=ffmpeg_path or Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe",
        ffprobe_path=ffprobe_path or Path(__file__).resolve().parent.parent / "bin" / "ffprobe.exe",
    )


async def _collect(gen):
    return [ev async for ev in gen]


async def test_pipeline_happy_path_emits_all_stages():
    p = _pipeline()
    events = await _collect(p.run(b"hello world", filename="x.md"))
    stages = [e.stage for e in events]
    # start + 4 stages (start+end each) + done
    assert "start" in stages
    assert stages.count("markdown_clean") == 2
    assert stages.count("llm_normalize") == 2
    assert stages.count("tts_synthesize") == 2
    assert stages.count("audio_save") == 2
    assert "done" in stages
    final = events[-1]
    assert final.stage == "done"
    assert final.progress == 1.0
    assert final.audio_id == "abc123"
    assert final.audio_url == "/api/audio/abc123"


async def test_pipeline_progress_is_monotonic_and_capped_at_1():
    p = _pipeline()
    events = await _collect(p.run(b"hello", filename="x.md"))
    progresses = [e.progress for e in events]
    assert all(0.0 <= x <= 1.0 for x in progresses)
    assert progresses == sorted(progresses)


async def test_pipeline_stage_endpoints_match_weights():
    p = _pipeline()
    events = await _collect(p.run(b"hello", filename="x.md"))
    by_stage = {}
    for e in events:
        by_stage.setdefault(e.stage, []).append(e.progress)
    # each stage appears twice: its start and its end, with end > start
    for stage in ("markdown_clean", "llm_normalize", "tts_synthesize", "audio_save"):
        starts, ends = by_stage[stage][0], by_stage[stage][1]
        assert ends > starts


async def test_pipeline_m3_failure_emits_error_and_terminates():
    p = _pipeline(llm=FakeLlm(fail=True))
    events = await _collect(p.run(b"hello", filename="x.md"))
    assert any(e.stage == "error" for e in events)
    assert not any(e.stage == "done" for e in events)
    assert not any(e.stage == "tts_synthesize" for e in events)


async def test_pipeline_tts_failure_emits_error_and_terminates():
    p = _pipeline(tts=FakeTts(fail=True))
    events = await _collect(p.run(b"hello", filename="x.md"))
    assert any(e.stage == "error" for e in events)
    # llm_normalize completed but tts did not.
    assert any(e.stage == "llm_normalize" for e in events)
    assert not any(e.stage == "audio_save" for e in events)
    assert not any(e.stage == "done" for e in events)


async def test_pipeline_empty_cleaned_text_emits_error():
    class EmptyMd:
        def to_plain_text(self, t): return ""
    p = _pipeline(md=EmptyMd())
    events = await _collect(p.run(b"hello", filename="x.md"))
    assert any(e.stage == "error" for e in events)
    assert not any(e.stage == "llm_normalize" for e in events)


async def test_pipeline_sse_format_is_data_lines():
    """Each ProgressEvent.to_sse() must produce parseable data: lines."""
    p = _pipeline()
    events = await _collect(p.run(b"hello", filename="x.md"))
    import json as _json
    for e in events:
        s = e.to_sse()
        assert s.startswith("data: ")
        # The JSON after "data: " should round-trip.
        body = _json.loads(s[6:].strip())
        assert body["stage"] == e.stage
        assert body["progress"] == e.progress


async def test_pipeline_voice_passed_through_to_done():
    p = _pipeline()
    events = await _collect(p.run(b"hi", filename="x.md", voice_id="冰糖", default_voice_id="mimo_default"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.voice_id == "冰糖"


async def test_pipeline_falls_back_to_default_voice():
    p = _pipeline()
    events = await _collect(p.run(b"hi", filename="x.md", voice_id=None, default_voice_id="mimo_default"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.voice_id == "mimo_default"