"""Unit tests for env-driven accessor functions in ``app.config``.

覆盖：
    * 默认值（未设 env）→ 返回模块级常量
    * env 覆盖 → accessor 返回 env 值
    * JSON 解析错误 → 兜底默认
    * lru_cache + reset_settings_cache
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from app.config import (
    M3_SYSTEM_PROMPT,
    MINIMAX_VOICES_ZH,
    SEMANTIC_PREPROCESS_PROMPT,
    SPLIT_SYSTEM_PROMPT,
    EdgeTtsSettings,
    get_edge_voices,
    get_m3_system_prompt,
    get_minimax_voices,
    get_semantic_preprocess_prompt,
    get_split_system_prompt,
    reset_settings_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache_between_tests():
    """每个测试前后清空 lru_cache，避免 env 污染。"""
    reset_settings_cache()
    yield
    # 测试后再清一次，并把改动过的 env 还原。
    reset_settings_cache()
    for key in (
        "APP__M3_SYSTEM_PROMPT",
        "APP__SPLIT_SYSTEM_PROMPT",
        "APP__SEMANTIC_PREPROCESS_PROMPT",
        "APP__MIMO_VOICES_JSON",
        "APP__EDGE_VOICES_JSON",
    ):
        os.environ.pop(key, None)


# ---- get_m3_system_prompt -------------------------------------------------


def test_get_m3_system_prompt_default():
    assert get_m3_system_prompt() == M3_SYSTEM_PROMPT


def test_get_m3_system_prompt_env_override(monkeypatch: pytest.MonkeyPatch):
    new_prompt = "CUSTOM M3 PROMPT FOR TEST"
    monkeypatch.setenv("APP__M3_SYSTEM_PROMPT", new_prompt)
    reset_settings_cache()
    assert get_m3_system_prompt() == new_prompt


def test_get_m3_system_prompt_env_empty_string_treated_as_none(monkeypatch):
    # 空字符串 pydantic-settings 可能存为 ""；accessor 视为"未覆盖"
    monkeypatch.setenv("APP__M3_SYSTEM_PROMPT", "")
    reset_settings_cache()
    assert get_m3_system_prompt() == M3_SYSTEM_PROMPT


# ---- get_split_system_prompt ---------------------------------------------


def test_get_split_system_prompt_default():
    assert get_split_system_prompt() == SPLIT_SYSTEM_PROMPT


def test_get_split_system_prompt_env_override(monkeypatch: pytest.MonkeyPatch):
    # 保留 {max_chars} 占位符才能 .format()
    new_prompt = "CUSTOM SPLIT up to {max_chars} chars"
    monkeypatch.setenv("APP__SPLIT_SYSTEM_PROMPT", new_prompt)
    reset_settings_cache()
    got = get_split_system_prompt()
    assert got == new_prompt
    assert "chars" in got.format(max_chars=1000)


# ---- get_semantic_preprocess_prompt -------------------------------------


def test_get_semantic_preprocess_prompt_default():
    assert get_semantic_preprocess_prompt() == SEMANTIC_PREPROCESS_PROMPT


def test_get_semantic_preprocess_prompt_env_override(monkeypatch: pytest.MonkeyPatch):
    new_prompt = "CUSTOM SEMANTIC"
    monkeypatch.setenv("APP__SEMANTIC_PREPROCESS_PROMPT", new_prompt)
    reset_settings_cache()
    assert get_semantic_preprocess_prompt() == new_prompt


# ---- get_minimax_voices ---------------------------------------------------


def test_get_minimax_voices_default_returns_static_list():
    voices = get_minimax_voices()
    assert isinstance(voices, list)
    # 默认应当含一些真实 voice_id
    ids = {v["id"] for v in voices}
    assert "male-qn-qingse" in ids
    # 默认条数等于 MINIMAX_VOICES_ZH
    assert len(voices) == len(MINIMAX_VOICES_ZH)


def test_get_minimax_voices_env_override(monkeypatch: pytest.MonkeyPatch):
    custom = json.dumps([
        {"id": "test_voice_1", "name": "测试 1", "lang": "zh"},
        {"id": "test_voice_2", "name": "测试 2", "lang": "en"},
    ])
    monkeypatch.setenv("APP__MINIMAX_VOICES_JSON", custom)
    reset_settings_cache()
    voices = get_minimax_voices()
    assert len(voices) == 2
    assert voices[0]["id"] == "test_voice_1"
    assert voices[1]["lang"] == "en"


def test_get_minimax_voices_env_invalid_json_falls_back(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("APP__MINIMAX_VOICES_JSON", "{not valid json")
    reset_settings_cache()
    with caplog.at_level("WARNING"):
        voices = get_minimax_voices()
    # 兜底默认
    assert len(voices) == len(MINIMAX_VOICES_ZH)
    assert "APP__MINIMAX_VOICES_JSON 解析失败" in caplog.text


def test_get_minimax_voices_env_not_list_falls_back(monkeypatch: pytest.MonkeyPatch, caplog):
    """JSON 合法但不是 list → 兜底。"""
    monkeypatch.setenv("APP__MINIMAX_VOICES_JSON", json.dumps({"id": "x"}))
    reset_settings_cache()
    with caplog.at_level("WARNING"):
        voices = get_minimax_voices()
    assert len(voices) == len(MINIMAX_VOICES_ZH)
    assert "回落默认" in caplog.text


# ---- get_edge_voices -----------------------------------------------------


def test_get_edge_voices_default_returns_zh_list():
    voices = get_edge_voices()
    assert isinstance(voices, list) and len(voices) == 13
    ids = {v["id"] for v in voices}
    assert "zh-CN-XiaoxiaoNeural" in ids


def test_get_edge_voices_env_override(monkeypatch: pytest.MonkeyPatch):
    custom = json.dumps([
        {"id": "en-US-AriaNeural", "name": "Aria", "lang": "en-US"},
    ])
    monkeypatch.setenv("APP__EDGE_VOICES_JSON", custom)
    reset_settings_cache()
    voices = get_edge_voices()
    assert len(voices) == 1
    assert voices[0]["id"] == "en-US-AriaNeural"


def test_get_edge_voices_env_invalid_json_falls_back(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("APP__EDGE_VOICES_JSON", "[oops")
    reset_settings_cache()
    with caplog.at_level("WARNING"):
        voices = get_edge_voices()
    assert len(voices) == 13
    assert "APP__EDGE_VOICES_JSON 解析失败" in caplog.text


# ---- lru_cache + reset ---------------------------------------------------


def test_accessors_are_cached(monkeypatch: pytest.MonkeyPatch):
    """连续两次调用应当返回**同一对象**（lru_cache 命中）。"""
    reset_settings_cache()
    a = get_minimax_voices()
    b = get_minimax_voices()
    # list() 在 accessor 内部 list(...) 已复制，但 lru_cache 仍缓存 list 对象
    # 即使内容相等也不一定是同一对象；这里只验"内容一致"
    assert a == b
    # 修改 env + reset 之前：accessor 仍返回旧值
    monkeypatch.setenv("APP__MINIMAX_VOICES_JSON", json.dumps([{"id": "x", "name": "x", "lang": "x"}]))
    c = get_minimax_voices()
    assert c == a  # 缓存命中，未重读 env
    # reset 后再读：拿到 env 覆盖
    reset_settings_cache()
    d = get_minimax_voices()
    assert len(d) == 1 and d[0]["id"] == "x"


def test_reset_settings_cache_clears_all(monkeypatch: pytest.MonkeyPatch):
    """reset_settings_cache 清掉 _settings + mimo + edge 三个 cache。"""
    monkeypatch.setenv("APP__M3_SYSTEM_PROMPT", "first")
    reset_settings_cache()
    assert get_m3_system_prompt() == "first"
    # 改 env，不 reset
    monkeypatch.setenv("APP__M3_SYSTEM_PROMPT", "second")
    assert get_m3_system_prompt() == "first"  # 缓存
    # reset 后重读
    reset_settings_cache()
    assert get_m3_system_prompt() == "second"


# ---- ffmpeg / ffprobe 子进程超时（env 化） -------------------------------


@pytest.fixture(autouse=False)
def _clean_ffmpeg_env():
    """单独用例前后清掉 3 个 ffmpeg/ffprobe 超时 env。"""
    for k in (
        "EDGE__FFPROBE_TIMEOUT_SEC",
        "EDGE__FFMPEG_CONCAT_TIMEOUT_SEC",
        "EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC",
    ):
        os.environ.pop(k, None)
    reset_settings_cache()
    yield
    for k in (
        "EDGE__FFPROBE_TIMEOUT_SEC",
        "EDGE__FFMPEG_CONCAT_TIMEOUT_SEC",
        "EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC",
    ):
        os.environ.pop(k, None)
    reset_settings_cache()


def test_edge_settings_ffprobe_default():
    s = EdgeTtsSettings()
    assert s.ffprobe_timeout_sec == 10.0
    assert s.ffmpeg_concat_timeout_sec == 120.0
    assert s.mimo_ffmpeg_concat_timeout_sec == 600.0


def test_edge_settings_ffprobe_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGE__FFPROBE_TIMEOUT_SEC", "3.5")
    s = EdgeTtsSettings()
    assert s.ffprobe_timeout_sec == 3.5


def test_edge_settings_ffmpeg_concat_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGE__FFMPEG_CONCAT_TIMEOUT_SEC", "777")
    s = EdgeTtsSettings()
    assert s.ffmpeg_concat_timeout_sec == 777.0


def test_edge_settings_mimo_ffmpeg_concat_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC", "1234")
    s = EdgeTtsSettings()
    assert s.mimo_ffmpeg_concat_timeout_sec == 1234.0


def test_edge_settings_ge_validator(monkeypatch: pytest.MonkeyPatch):
    """ge=1.0 校验：0 或负值应当被 pydantic 拒绝。"""
    from pydantic import ValidationError
    monkeypatch.setenv("EDGE__FFPROBE_TIMEOUT_SEC", "0")
    with pytest.raises(ValidationError):
        EdgeTtsSettings()
    monkeypatch.setenv("EDGE__FFPROBE_TIMEOUT_SEC", "-1")
    with pytest.raises(ValidationError):
        EdgeTtsSettings()


def test_probe_audio_duration_uses_settings_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """probe_audio_duration 应当把 timeout_sec 透传给 subprocess.run。"""
    import subprocess
    from app.services.edge_tts_provider import probe_audio_duration

    fake_ffprobe = tmp_path / "ffprobe.exe"
    fake_ffprobe.write_text("")  # 存在即可；实际跑会被 timeout 触发
    audio = tmp_path / "fake.mp3"
    audio.write_text("x")

    captured = {}

    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        # 不真正执行 ffprobe；模拟快速成功
        class _R:
            returncode = 0
            stdout = '{"format":{"duration":1.5}}'
            stderr = ""
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    d = probe_audio_duration(audio, fake_ffprobe, timeout_sec=42.0)
    assert d == 1.5
    assert captured["timeout"] == 42.0


def test_pipeline_helper_reads_from_edge_settings():
    """TtsPipeline._edge_concat_timeout() 应当从 edge_settings 读。"""
    from app.services.pipeline import TtsPipeline
    es = EdgeTtsSettings(ffmpeg_concat_timeout_sec=99.0, mimo_ffmpeg_concat_timeout_sec=88.0)
    pipe = TtsPipeline(
         llm=None, audio=None,
        edge_tts=None, minimax_tts=None,
        ffmpeg_path=None, ffprobe_path=None,
        edge_settings=es,
    )
    assert pipe._probe_timeout() == 10.0  # default
    assert pipe._edge_concat_timeout() == 99.0
    assert pipe._minimax_concat_timeout() == 88.0


def test_pipeline_helper_falls_back_to_edge_client_settings():
    """没传 edge_settings 但 edge_tts._settings 有值时也读得到。"""
    from app.services.pipeline import TtsPipeline
    fake_client = MagicMock()
    fake_client._settings = EdgeTtsSettings(ffmpeg_concat_timeout_sec=55.0)
    pipe = TtsPipeline(
         llm=None, audio=None,
        edge_tts=fake_client, minimax_tts=None,
        ffmpeg_path=None, ffprobe_path=None,
    )
    assert pipe._edge_concat_timeout() == 55.0


def test_pipeline_helper_fallback_default():
    """既没 edge_settings 也没 edge_tts → 兜底默认。"""
    from app.services.pipeline import TtsPipeline
    pipe = TtsPipeline(
         llm=None, audio=None,
        edge_tts=None, minimax_tts=None,
        ffmpeg_path=None, ffprobe_path=None,
    )
    assert pipe._probe_timeout() == 10.0
    assert pipe._edge_concat_timeout() == 120.0
    assert pipe._minimax_concat_timeout() == 600.0
