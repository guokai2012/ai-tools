"""v4 端到端集成测试：edge-tts provider 完整流水线。

覆盖：
  1. LlmNormalizer.semantic_preprocess → EdgeTtsClient 分段合成 → ffmpeg 合并
  2. task_dir 下产物：<task_id>.mp3 / <task_id>.SRT / <task_id>.LRC
  3. ProgressEvent 携带 provider="edge"

注意：
  * EdgeTtsClient.synthesize_segment 被 monkeypatch 为返回"用真实 ffmpeg 生成的 mp3 字节"。
    既不需联网，又能让 ffprobe 探测到合法时长，触发 ffmpeg concat 合并。
  * LlmNormalizer.semantic_preprocess 被 mock 为返回固定文本，避免真实 M3 调用。
  * 测试用项目的 bin/ffmpeg.exe 与 bin/ffprobe.exe（已内置）。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import EdgeTtsSettings, PROVIDER_EDGE
from app.services.audio_storage import (
    AudioStorageService,
    TaskRecord,
    TaskStore,
    task_date_str,
)
from app.services.edge_tts_provider import EdgeTtsClient
from app.services.llm_normalizer import LlmNormalizer
from app.services.pipeline import TtsPipeline
from app.services.task_manager import TaskManager


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


# 预设 M3 语义预处理输出（含多音字 [拼音] 标记 + 句末标点）
SEMANTIC_OUTPUT = (
    "行长[zhǎng]得很快。\n\n"
    "这是一个测试句子。\n\n"
    "第二句也很短。"
)


# ---- 端到端测试 ------------------------------------------------------------


async def test_pipeline_edge_full_flow_emits_mp3_srt_lrc(tmp_path: Path):
    """端到端：上传 .md → edge pipeline → 断言 task_dir 下 mp3 + SRT + LRC 三件套。"""
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value=SEMANTIC_OUTPUT)

    audio_svc = AudioStorageService(tmp_path)
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
    segment_audio = _make_real_mp3_segment(duration_sec=0.5)

    async def fake_synthesize_segment(self, text, *, voice=None):
        cues = [(0.0, 0.5, text.strip())]
        return segment_audio, cues

    with patch.object(EdgeTtsClient, "synthesize_segment", new=fake_synthesize_segment):
        pipe = TtsPipeline(
            llm=llm_svc,
            audio=audio_svc,
            edge_tts=edge_client, ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
        )

        # 通过 TaskManager 走完整 create → convert 链路
        task_store = TaskStore(tmp_path / "lib.db")
        mgr = TaskManager(
            pipeline=pipe, task_store=task_store,
            audio_storage=audio_svc,
            llm=llm_svc,
        )
        task_id = mgr.create_task(b"# demo\n\nThis is a test markdown file.", filename="demo.md")
        rec = task_store.get(task_id)
        # 注入 normalized_text（实际 M3 跑过的语义预处理结果）
        task_store.update_progress(
            task_id, normalized_text=SEMANTIC_OUTPUT,
            status="ready_to_convert",
        )
        mgr.convert_task(task_id)
        # 等异步执行完成
        import asyncio
        await asyncio.sleep(1.5)

    rec_after = task_store.get(task_id)
    assert rec_after.status == "done"
    date_str = rec_after.date_str
    task_dir = audio_svc.task_dir(task_id, date_str=date_str)

    # ---- 断言 mp3 / SRT / LRC ----
    final_mp3 = task_dir / f"{task_id}.mp3"
    assert final_mp3.exists(), f"mp3 missing: {final_mp3}"
    mp3_dur = _ffprobe_duration(final_mp3)
    assert mp3_dur > 0.0, "ffprobe should detect non-zero duration"

    final_srt = task_dir / f"{task_id}.SRT"
    assert final_srt.exists()
    srt_text = final_srt.read_text(encoding="utf-8")
    srt_entries = re.findall(r"^\d+$", srt_text, re.MULTILINE)
    assert len(srt_entries) >= 3, f"expected >=3 srt entries, got {len(srt_entries)}"
    srt_timestamps = re.findall(
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", srt_text,
    )
    assert len(srt_timestamps) >= 6

    final_lrc = task_dir / f"{task_id}.LRC"
    assert final_lrc.exists()
    lrc_text = final_lrc.read_text(encoding="utf-8")
    # LRC 含 [ti:demo] 头
    assert f"[ti:{task_id[:8]}]" in lrc_text, "LRC should have title from task_id[:8]"
    assert "[ar:txt2tts]" in lrc_text
    # 歌词行
    assert "行长[zhǎng]" not in lrc_text
    assert "行长" in lrc_text
    assert "测试句子" in lrc_text
    lrc_ts = re.findall(r"\[(\d{2}):(\d{2})\.(\d{2})\]", lrc_text)
    assert len(lrc_ts) >= 3


async def test_pipeline_edge_segments_dir_cleanup(tmp_path: Path):
    """端到端跑完后，task_dir 下 split_<N>.mp3 / <task_id>.mp3 都在；segments/ 不存在（v4 不再单独存）。"""
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value="一句。")

    audio_svc = AudioStorageService(tmp_path)
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))

    segment_audio = _make_real_mp3_segment(duration_sec=0.3)

    async def fake_segment(self, text, *, voice=None):
        return segment_audio, [(0.0, 0.3, text.strip())]

    with patch.object(EdgeTtsClient, "synthesize_segment", new=fake_segment):
        pipe = TtsPipeline(
            llm=llm_svc,
            audio=audio_svc,
            edge_tts=edge_client, ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
        )
        task_store = TaskStore(tmp_path / "lib.db")
        mgr = TaskManager(
            pipeline=pipe, task_store=task_store,
            audio_storage=audio_svc,
            llm=llm_svc,
        )
        task_id = mgr.create_task(b"# x", filename="a.md")
        task_store.update_progress(task_id, normalized_text="一句。", status="ready_to_convert")
        mgr.convert_task(task_id)
        import asyncio
        await asyncio.sleep(1.5)

    rec = task_store.get(task_id)
    task_dir = audio_svc.task_dir(task_id, date_str=rec.date_str)
    # task_dir 下应有：<task_id>.mp3 + split_<N>.md + split_<N>.mp3 + ...
    assert (task_dir / f"{task_id}.mp3").exists()
    # 旧的 segments/ 目录不存在
    assert not (tmp_path / "segments").exists()


async def test_pipeline_edge_provider_field_on_every_event(tmp_path: Path):
    """所有 ProgressEvent 都应携带 provider='edge' 字段（前端识别用）。"""
    llm_svc = AsyncMock(spec=LlmNormalizer)
    llm_svc.semantic_preprocess = AsyncMock(return_value="测试句子。")
    audio_svc = AudioStorageService(tmp_path)
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))
    seg = _make_real_mp3_segment(duration_sec=0.3)

    async def fake_segment(self, text, *, voice=None):
        return seg, [(0.0, 0.3, text.strip())]

    with patch.object(EdgeTtsClient, "synthesize_segment", new=fake_segment):
        pipe = TtsPipeline(
            llm=llm_svc,
            audio=audio_svc,
            edge_tts=edge_client, ffmpeg_path=FFMPEG,
            ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
        )
        # 通过 pipeline.run 直接收集 events（不通过 TaskManager）
        events = []
        async for ev in pipe.run(b"# x", filename="x.md"):
            events.append(ev)

    # 所有 event 都带 provider='edge'
    for e in events:
        assert getattr(e, "provider", None) == "edge", \
            f"event {e.stage} missing provider='edge'"


async def test_pipeline_edge_handles_semantic_preprocess_failure(tmp_path: Path):
    """M3 语义预处理失败时，pipeline 应当 yield error event 而不抛异常。"""
    llm_svc = AsyncMock(spec=LlmNormalizer)
    from app.services.llm_normalizer import LlmNormalizationError
    llm_svc.semantic_preprocess = AsyncMock(side_effect=LlmNormalizationError("m3 down"))

    audio_svc = AudioStorageService(tmp_path)
    edge_client = EdgeTtsClient(EdgeTtsSettings(ffmpeg_path=str(FFMPEG)))

    pipe = TtsPipeline(
        llm=llm_svc,
        audio=audio_svc,
        edge_tts=edge_client, ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE, provider=PROVIDER_EDGE,
    )

    events = []
    async for ev in pipe.run(b"# x", filename="x.md"):
        events.append(ev)

    error_events = [e for e in events if e.stage == "error"]
    assert len(error_events) == 1
    assert "m3 down" in error_events[0].error
    # 不应产生 mp3（pipeline 提早失败）
    assert not (tmp_path / "audio").exists()
    assert not list(tmp_path.rglob("*.mp3"))