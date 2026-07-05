"""Unit tests for the TtsPipeline progress generator (minimax provider only).

v4 起 mimo provider 已移除，pipeline 默认走 minimax。FakeTts 改为
FakeMinimaxTts，模拟 MinimaxTtsClient.synthesize_segment 返回 ProviderResult。

v5 起不再有 MarkdownService：原始 MD 直接喂 M3，pipeline.run 第一个阶段
就是 llm_normalize（不再有 markdown_clean）。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.services.audio_storage import AudioStorageService
from app.services.llm_normalizer import LlmNormalizationError, LlmNormalizer
from app.services.minimax_tts_provider import (
    MinimaxTtsError,
    ProviderResult,
    SubtitleFetchError,
)
from app.services.pipeline import ProgressEvent, TtsPipeline


pytestmark = pytest.mark.asyncio


class FakeLlm:
    def __init__(self, fail: bool = False, text: str = "NORMALIZED"):
        self._fail = fail
        self._text = text

    async def normalize(self, t: str) -> str:
        if self._fail:
            raise LlmNormalizationError("boom")
        return self._text


class FakeMinimaxTts:
    """模拟 MinimaxTtsClient.synthesize_segment，返回 ProviderResult。"""

    def __init__(self, fail: bool = False, audio: bytes = None,
                 subtitle_error: str = None, duration_sec: float = 1.0):
        self._fail = fail
        self._audio = audio if audio is not None else _make_real_mp3(0.3)
        self._subtitle_error = subtitle_error
        self._duration_sec = duration_sec
        # 模拟 MinimaxTtsSettings 的关键字段
        self._settings = SimpleNamespace(max_input_chars_per_request=10000)

    async def synthesize_segment(self, text: str, *, voice=None, title="", artist="txt2tts"):
        if self._fail:
            raise MinimaxTtsError("tts boom")
        cues = []
        if not self._subtitle_error:
            cues = [(0.0, 0.5, "第一句"), (0.5, 1.0, "第二句")]
        return ProviderResult(
            audio_bytes=self._audio,
            duration_sec=self._duration_sec,
            srt_text="1\n00:00:00,000 --> 00:00:00,500\n第一句\n\n2\n00:00:00,500 --> 00:00:01,000\n第二句\n",
            lrc_text="[ti:test]\n[ar:txt2tts]\n[00:00.00]第一句\n[00:00.50]第二句\n",
            sentence_cues=cues,
            subtitle_fetch_error=self._subtitle_error,
        )


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

    def task_dir(self, task_id, *, date_str=None):
        from app.services.audio_storage import task_date_str
        d = date_str or task_date_str()
        p = self._root / d / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def task_file_path(self, task_id, filename, *, date_str=None):
        return self.task_dir(task_id, date_str=date_str) / filename

    def resolve(self, task_id, *, date_str=None):
        p = self.task_file_path(task_id, f"{task_id}.mp3", date_str=date_str)
        return p if p.exists() else None


def _pipeline(llm=None, minimax_tts=None, audio=None,
              ffmpeg_path=None, ffprobe_path=None) -> TtsPipeline:
    from pathlib import Path
    return TtsPipeline(
        llm=llm or FakeLlm(),
        audio=audio or FakeAudio(),
        minimax_tts=minimax_tts or FakeMinimaxTts(),
        ffmpeg_path=ffmpeg_path or Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe",
        ffprobe_path=ffprobe_path or Path(__file__).resolve().parent.parent / "bin" / "ffprobe.exe",
        provider="minimax",
    )


async def _collect(gen):
    return [ev async for ev in gen]


async def test_pipeline_happy_path_emits_all_stages():
    p = _pipeline()
    events = await _collect(p.run(b"hello world", filename="x.md"))
    stages = [e.stage for e in events]
    assert "start" in stages
    # v5 起去 markdown_clean：3 阶段 llm_normalize / tts_synthesize / audio_save
    assert stages.count("llm_normalize") == 2
    assert stages.count("tts_synthesize") == 2
    assert stages.count("audio_save") == 2
    assert "done" in stages
    final = events[-1]
    assert final.stage == "done"
    assert final.progress == 1.0
    # v4：无 task_id 时 audio_id/audio_url 为 None（pipeline.run 入口走端到端测试）
    assert final.audio_id is None
    assert final.audio_url is None
    # minimax provider: done event 应带 subtitle_status="ok"
    assert final.subtitle_status == "ok"


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
    for stage in ("llm_normalize", "tts_synthesize", "audio_save"):
        starts, ends = by_stage[stage][0], by_stage[stage][1]
        assert ends > starts


async def test_pipeline_m3_failure_emits_error_and_terminates():
    p = _pipeline(llm=FakeLlm(fail=True))
    events = await _collect(p.run(b"hello", filename="x.md"))
    assert any(e.stage == "error" for e in events)
    assert not any(e.stage == "done" for e in events)
    assert not any(e.stage == "tts_synthesize" for e in events)


async def test_pipeline_tts_failure_emits_error_and_terminates():
    p = _pipeline(minimax_tts=FakeMinimaxTts(fail=True))
    events = await _collect(p.run(b"hello", filename="x.md"))
    assert any(e.stage == "error" for e in events)
    assert any(e.stage == "llm_normalize" for e in events)
    assert not any(e.stage == "audio_save" for e in events)
    assert not any(e.stage == "done" for e in events)


async def test_pipeline_empty_document_emits_error():
    """v5 起：原始 bytes decode 后空文本 → error 事件，不再有 EmptyMd 路径。"""
    p = _pipeline()
    events = await _collect(p.run(b"   \n\n  ", filename="x.md"))
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
        body = _json.loads(s[6:].strip())
        assert body["stage"] == e.stage
        assert body["progress"] == e.progress


async def test_pipeline_voice_passed_through_to_done():
    p = _pipeline()
    events = await _collect(p.run(b"hi", filename="x.md",
                                    voice_id="female-shaonv", default_voice_id="male-qn-qingse"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.voice_id == "female-shaonv"


async def test_pipeline_falls_back_to_default_voice():
    p = _pipeline()
    events = await _collect(p.run(b"hi", filename="x.md",
                                    voice_id=None, default_voice_id="male-qn-qingse"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.voice_id == "male-qn-qingse"


async def test_pipeline_done_event_carries_subtitle_status_ok():
    """minimax provider + 字幕成功：done event subtitle_status='ok'。"""
    p = _pipeline()
    events = await _collect(p.run(b"hi", filename="x.md"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.subtitle_status == "ok"
    assert done.subtitle_error is None


async def test_pipeline_done_event_carries_subtitle_status_pending():
    """minimax provider + 字幕拉取失败：done event subtitle_status='pending'。"""
    p = _pipeline(minimax_tts=FakeMinimaxTts(subtitle_error="OSS URL 404"))
    events = await _collect(p.run(b"hi", filename="x.md"))
    done = [e for e in events if e.stage == "done"][0]
    assert done.subtitle_status == "pending"
    assert "404" in (done.subtitle_error or "")