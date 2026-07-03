"""端到端集成测试：edge-tts provider 完整流水线。

覆盖：
  1. LlmNormalizer.semantic_preprocess（含多音字标记 + 句末标点）→ EdgeTtsClient 分段合成 → ffmpeg 合并
  2. 三件套产物：mp3 / srt / lrc 路径与内容正确
  3. LibraryStore 写入 provider="edge"（lyrics_path 不再回写，转歌词功能已移除）
  4. ProgressEvent 携带 provider 字段

注意：
  * EdgeTtsClient.synthesize_segment 被 monkeypatch 为返回"用真实 ffmpeg 生成的 mp3 字节"。
    这样既不需要联网，又能让 ffprobe 探测到合法时长，触发 ffmpeg concat 合并。
  * LlmNormalizer.semantic_preprocess 被 mock 为返回固定文本，避免真实 M3 调用。
  * 测试用项目的 bin/ffmpeg.exe 与 bin/ffprobe.exe（已内置）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import EdgeTtsSettings, PROVIDER_EDGE
from app.services.audio_storage import (
    AudioRecord,
    AudioStorageService,
    LibraryStore,
    SettingsStore,
)
from app.services.edge_tts_provider import EdgeTtsClient
from app.services.llm_normalizer import LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.pipeline import TtsPipeline


# ---- helpers --------------------------------------------------------------


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FFMPEG = PROJECT_ROOT / "bin" / "ffmpeg.exe"
FFPROBE = PROJECT_ROOT / "bin" / "ffprobe.exe"


def _make_real_mp3_segment(duration_sec: float = 0.5) -> bytes:
    """调用真实 ffmpeg 生成一个 440Hz 正弦波 mp3，让 ffprobe 能识别。"""
    if not FFMPEG.exists():
        pytest.skip(f"ffmpeg 不存在: {FFMPEG}")
    out = subprocess.run(
        [
            str(FFMPEG), "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_sec}",
            "-ar", "22050", "-ac", "1",
            "-codec:a", "libmp3lame", "-b:a", "64k",
            "-f", "mp3", "pipe:1",
        ],
        check=True, capture_output=True,
    )
    return out.stdout


def _ffprobe_duration(path: Path) -> float:
    """探测音频时长（秒）。"""
    out = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


# ---- Fixtures -------------------------------------------------------------


class FakeMd:
    def to_plain_text(self, t: str) -> str:
        return "行长[zhǎng]得很快。这是一个测试句子。第二句也很短。"


# 预设 M3 语义预处理输出（含多音字 [拼音] 标记 + 句末标点）
SEMANTIC_OUTPUT = (
    "行长[zhǎng]得很快。\n\n"
    "这是一个测试句子。\n\n"
    "第二句也很短。"
)


# ---- 测试 -----------------------------------------------------------------


async def test_pipeline_edge_full_flow_emits_mp3_srt_lrc(tmp_path: Path):
    """端到端：上传 .md → edge pipeline → 断言 mp3 + srt + lrc 三件套。"""
    # ---- 准备 services ----
    md_svc = FakeMd()
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value=SEMANTIC_OUTPUT)
    tts_client = AsyncMock()  # edge provider 不调用它，但 pipeline 构造时还是注入

    audio_svc = AudioStorageService(tmp_path)
    library_svc = LibraryStore(tmp_path / "lib.db")

    edge_settings = EdgeTtsSettings(
        ffmpeg_path=str(FFMPEG),
        ffprobe_path=str(FFPROBE),
        default_voice="zh-CN-XiaoxiaoNeural",
        rate="+0%",
        volume="+0%",
        pitch="+0Hz",
        max_segment_chars=200,
        request_timeout_sec=30.0,
    )
    edge_client = EdgeTtsClient(edge_settings)

    # ---- monkeypatch EdgeTtsClient.synthesize_segment ----
    # 每个 segment 返回一个 0.5s 的真实 mp3 + 单句时间戳
    segment_audio = _make_real_mp3_segment(duration_sec=0.5)

    async def fake_synthesize_segment(self, text, *, voice=None):
        """patch.object 会把方法当 unbound function 调用，第一个位置参数其实是 self。"""
        cues = [(0.0, 0.5, text.strip())]
        return segment_audio, cues

    with patch.object(
        EdgeTtsClient, "synthesize_segment",
        new=fake_synthesize_segment,
    ):
        pipe = TtsPipeline(
            markdown=md_svc,
            llm=llm_svc,
            tts=tts_client,
            audio=audio_svc,
            library=library_svc,
            edge_tts=edge_client,
            ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE,
            provider=PROVIDER_EDGE,
        )

        # ---- 收集 ProgressEvent ----
        events = []
        async for ev in pipe.run(
            b"# demo\n\nThis is a test markdown file.",
            filename="demo.md",
            voice_id="zh-CN-XiaoxiaoNeural",
            default_voice_id="zh-CN-XiaoxiaoNeural",
        ):
            events.append(ev)

    # ---- 断言 events ----
    stages = [e.stage for e in events]
    assert "start" in stages
    assert "markdown_clean" in stages
    assert "llm_normalize" in stages
    assert "tts_synthesize" in stages
    assert "audio_save" in stages
    assert "done" in stages
    assert not any(e.stage == "error" for e in events)

    done_event = next(e for e in events if e.stage == "done")
    audio_id = done_event.audio_id
    assert audio_id, "done event must carry audio_id"
    # provider 字段
    assert done_event.provider == "edge"
    assert all(getattr(e, "provider", None) == "edge" for e in events)

    # ---- 断言 mp3 三件套 ----
    # 1) AudioStorageService.save 落盘的 mp3
    saved_mp3 = audio_svc.resolve(audio_id)
    assert saved_mp3 is not None, f"mp3 not found for {audio_id}"
    assert saved_mp3.exists()
    assert saved_mp3.stat().st_size > 0
    # 必须是合法 mp3（ffprobe 能读时长）
    mp3_dur = _ffprobe_duration(saved_mp3)
    assert mp3_dur > 0.0, "ffprobe should detect non-zero duration"

    # 2) SRT 字幕（v2 写到 audio/_artifacts/<audio_id>/）
    srt_path = tmp_path / "audio" / "_artifacts" / audio_id / f"{audio_id}.srt"
    assert srt_path.exists(), f"srt missing: {srt_path}"
    srt_text = srt_path.read_text(encoding="utf-8")
    # 至少要有 3 句
    import re
    srt_entries = re.findall(r"^\d+$", srt_text, re.MULTILINE)
    assert len(srt_entries) >= 3, f"expected >=3 srt entries, got {len(srt_entries)}"
    # SRT 时间戳递增
    srt_timestamps = re.findall(
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", srt_text,
    )
    assert len(srt_timestamps) >= 6  # 3 entries × 2 (start, end)

    # 3) LRC 歌词
    lrc_path = srt_path.with_suffix(".lrc")
    assert lrc_path.exists(), f"lrc missing: {lrc_path}"
    lrc_text = lrc_path.read_text(encoding="utf-8")
    assert "[ti:demo]" in lrc_text, "LRC header should have title from filename stem"
    assert "[ar:txt2tts]" in lrc_text
    # 含歌词行（多音字已被剥离）
    assert "行长[zhǎng]" not in lrc_text, "LRC should strip disambiguation marks"
    assert "行长" in lrc_text
    assert "测试句子" in lrc_text
    # LRC 时间戳格式
    lrc_ts = re.findall(r"\[(\d{2}):(\d{2})\.(\d{2})\]", lrc_text)
    assert len(lrc_ts) >= 3, f"expected >=3 lrc time tags, got {len(lrc_ts)}"

    # ---- 断言 LibraryStore ----
    # 转歌词功能已移除：pipeline 不再把 lyrics_path 回写 audio_records。
    # LibraryStore.lyrics_path 字段保留以兼容旧数据；本任务应当为 None。
    record = library_svc.get(audio_id)
    assert record is not None
    assert record.provider == "edge"
    assert record.lyrics_path is None
    assert record.original_filename == "demo.md"
    # 写入的是 normalized（剥离多音字标记的版本由 pipeline 直接用作为朗读文本，
    # 但存到库的 normalized_md 保留语义预处理结果）
    assert "行长" in record.normalized_md


async def test_pipeline_edge_segments_dir_cleanup(tmp_path: Path):
    """端到端跑完后，临时片段目录应该保留（供调试），但完整 mp3 已生成。"""
    md_svc = FakeMd()
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value="一句。")

    audio_svc = AudioStorageService(tmp_path)
    library_svc = LibraryStore(tmp_path / "lib.db")
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))

    segment_audio = _make_real_mp3_segment(duration_sec=0.3)

    async def fake_segment(self, text, *, voice=None):
        return segment_audio, [(0.0, 0.3, text.strip())]

    with patch.object(EdgeTtsClient, "synthesize_segment", new=fake_segment):
        pipe = TtsPipeline(
            markdown=md_svc, llm=llm_svc, tts=AsyncMock(),
            audio=audio_svc, library=library_svc,
            edge_tts=edge_client, ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
        )
        events = []
        async for ev in pipe.run(b"raw md", filename="a.md"):
            events.append(ev)

    done = next(e for e in events if e.stage == "done")
    aid = done.audio_id
    assert audio_svc.resolve(aid) is not None
    # 至少存在 segments/<temp_id>/*.mp3 临时片段
    seg_root = tmp_path / "segments"
    assert seg_root.exists()
    seg_dirs = list(seg_root.iterdir())
    assert seg_dirs, "segments dir should contain at least one task subdir"
    # 临时 mp3 文件存在
    found = list(seg_dirs[0].glob("*.mp3"))
    assert found, "no segment mp3 in temp dir"


async def test_pipeline_edge_provider_field_on_every_event(tmp_path: Path):
    """所有 ProgressEvent 都应携带 provider='edge' 字段（前端识别用）。"""
    md_svc = FakeMd()
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value="测试句子。")
    audio_svc = AudioStorageService(tmp_path)
    library_svc = LibraryStore(tmp_path / "lib.db")
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))
    seg = _make_real_mp3_segment(duration_sec=0.3)

    async def fake_segment(self, text, *, voice=None):
        return seg, [(0.0, 0.3, text.strip())]

    with patch.object(EdgeTtsClient, "synthesize_segment", new=fake_segment):
        pipe = TtsPipeline(
            markdown=md_svc, llm=llm_svc, tts=AsyncMock(),
            audio=audio_svc, library=library_svc,
            edge_tts=edge_client, ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
        )
        events = []
        async for ev in pipe.run(b"# x", filename="x.md"):
            events.append(ev)

    # 所有 event 都带 provider='edge'
    for e in events:
        assert getattr(e, "provider", None) == "edge", \
            f"event {e.stage} missing provider='edge'"


async def test_pipeline_edge_handles_semantic_preprocess_failure(tmp_path: Path):
    """M3 语义预处理失败时，pipeline 应当 yield error event 而不抛异常。"""
    md_svc = FakeMd()
    llm_svc = AsyncMock(spec=LlmNormalizer)
    from app.services.llm_normalizer import LlmNormalizationError
    llm_svc.semantic_preprocess = AsyncMock(
        side_effect=LlmNormalizationError("m3 down"),
    )

    audio_svc = AudioStorageService(tmp_path)
    library_svc = LibraryStore(tmp_path / "lib.db")
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))

    pipe = TtsPipeline(
        markdown=md_svc, llm=llm_svc, tts=AsyncMock(),
        audio=audio_svc, library=library_svc,
        edge_tts=edge_client, ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
    )

    events = []
    async for ev in pipe.run(b"# x", filename="x.md"):
        events.append(ev)

    error_events = [e for e in events if e.stage == "error"]
    assert len(error_events) == 1
    assert "m3 down" in error_events[0].error
    # 不应产生 mp3 / srt / lrc（pipeline 提早失败 → 没机会落盘）
    assert not (tmp_path / "audio").exists() or \
        not list((tmp_path / "audio" / "_artifacts").rglob("*.lrc"))