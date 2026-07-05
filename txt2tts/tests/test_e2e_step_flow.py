"""v4 端到端测试：分步交互式任务流的 4 步。

使用 FastAPI TestClient 真实发出 HTTP 请求，验证：
1. POST /api/tasks 创建草稿任务 → task_dir/<task_id>.md 立即落盘
2. POST /api/tasks/{id}/normalize 触发 M3 标准化（mock LLM）→ normalization.md
3. POST /api/tasks/{id}/split 按 chapter 预设拆分 → split_<N>.md
4. POST /api/tasks/{id}/confirm-split 确认子文档
5. POST /api/tasks/{id}/convert 启动 TTS 转换（mock TTS）→ <task_id>.mp3/SRT/LRC
6. 听文档 = GET /api/library（status='done' 的任务）

v4：无 audio_id；URL 一律用 task_id；所有产物在 task_dir 下。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import create_app
from app.services.llm_normalizer import LlmNormalizer
from app.services.minimax_tts_provider import MinimaxTtsClient, ProviderResult
import subprocess


MOCK_MP3 = b"\x49\x44\x33" + b"\x00" * 100 + b"\xff\xfb\x90\x00" + b"\x00" * 50


@pytest.fixture
def e2e_env(tmp_path: Path, monkeypatch):
    """构造隔离的 e2e 环境：临时 outputs + mock LLM/TTS。

    设置环境变量：让 AppSettings 走临时目录；mock LLM/TTS；启动服务前 reload。
    """
    out = tmp_path / "outputs"
    uploads = out / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    md_content = b"# Title\n\n## A\n\nContent A.\n\n## B\n\nContent B."

    import os
    old_env = {}
    for k, v in {
        "APP__OUTPUT_DIR": str(out.resolve()),
        "APP__LIBRARY_DB_FILENAME": "library.db",
        "APP__TASK_WATCHDOG_ENABLED": "false",
        "LLM__API_KEY": "test-llm-key",
        "MINIMAX__API_KEY": "test-minimax-key",
    }.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    # 重新加载 config（pydantic-settings 启动时已冻结）
    import importlib
    import app.config as cfg_mod
    importlib.reload(cfg_mod)
    import app.services.audio_storage as audio_mod
    importlib.reload(audio_mod)
    import app.services.task_manager as tm_mod
    importlib.reload(tm_mod)
    import app.services.pipeline as pipe_mod
    importlib.reload(pipe_mod)
    import app.routers.tts as router_mod
    importlib.reload(router_mod)
    import app.main as main_mod
    importlib.reload(main_mod)

    yield {"out_dir": out, "md_content": md_content}

    # 恢复环境
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # 再次 reload 回到原 settings
    importlib.reload(cfg_mod)
    importlib.reload(audio_mod)
    importlib.reload(tm_mod)
    importlib.reload(pipe_mod)
    importlib.reload(router_mod)
    importlib.reload(main_mod)


def _wait_until(client, task_id, target_status, *, timeout_s=10.0, interval_s=0.2):
    """轮询直到 status=target_status 或超时。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/tasks/{task_id}").json()
        if last.get("status") == target_status:
            return last
        time.sleep(interval_s)
    raise AssertionError(
        f"任务 {task_id} 在 {timeout_s}s 内未到 {target_status}，最后：{last}"
    )


def _make_real_mp3(duration_sec: float = 0.3) -> bytes:
    """用真 ffmpeg 生成 440Hz 正弦波 mp3，让 ffmpeg concat 能识别。"""
    from pathlib import Path
    ffmpeg = Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        pytest.skip(f"ffmpeg 不存在: {ffmpeg}")
    out = subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={duration_sec}",
         "-ar", "22050", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "64k",
         "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    )
    return out.stdout


def _mock_tts(monkeypatch):
    """mock MinimaxTtsClient.synthesize_segment 返回带 cues 的 ProviderResult。

    使用 ffmpeg 生成合法 mp3 字节，让 _ffmpeg_concat 真跑也能成功（避免 instance method
    monkeypatch 不生效的问题）。
    """
    real_segment = _make_real_mp3(0.3)

    async def fake_synthesize_segment(self, text, *, voice=None, title="", artist="txt2tts"):
        return ProviderResult(
            audio_bytes=real_segment,
            duration_sec=0.3,
            srt_text="1\n00:00:00,000 --> 00:00:00,500\n第一句\n\n2\n00:00:00,500 --> 00:00:01,000\n第二句\n",
            lrc_text="",
            sentence_cues=[(0.0, 0.3, "第一句"), (0.3, 0.6, "第二句")],
            subtitle_fetch_error=None,
        )
    monkeypatch.setattr(MinimaxTtsClient, "synthesize_segment", fake_synthesize_segment)


def test_e2e_step_flow_with_chapter_preset(e2e_env, monkeypatch):
    """完整 4 步：upload → normalize → split(章节) → confirm → convert → done。"""
    app = create_app()

    # mock LLM
    async def fake_normalize(self, text, *, system=None):
        return f"已标准化：{text[:50]}..."

    async def fake_split_text(self, text, *, max_chars=6000, system=None):
        if system and "章节" in system:
            return ["第一章节内容", "第二章节内容", "第三章节内容"]
        return [text]

    monkeypatch.setattr(LlmNormalizer, "normalize", fake_normalize)
    monkeypatch.setattr(LlmNormalizer, "split_text", fake_split_text)
    _mock_tts(monkeypatch)

    with TestClient(app) as client:
        # 1) 上传
        r = client.post("/api/tasks", files={"file": ("demo.md", e2e_env["md_content"], "text/markdown")})
        assert r.status_code == 200, r.text
        tid = r.json()["task_id"]
        assert len(tid) == 32
        assert r.json()["message"] == "草稿任务已创建"

        # 验证 draft 状态 + task_dir/<task_id>.md 落盘
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "draft"
        assert rec["date_str"] and len(rec["date_str"]) == 8
        assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"{tid}.md").exists()

        # 2) 触发 M3 标准化（异步）→ ready_to_split
        r = client.post(f"/api/tasks/{tid}/normalize")
        assert r.status_code == 200
        rec = _wait_until(client, tid, "ready_to_split", timeout_s=5.0)
        assert rec["normalized_text"]
        # normalization.md 写盘
        assert (e2e_env["out_dir"] / rec["date_str"] / tid / "normalization.md").exists()

        # 3) 按 chapter 预设拆分
        r = client.get("/api/split-presets")
        chapter = next(p for p in r.json() if p["id"] == "chapter")
        r = client.post(f"/api/tasks/{tid}/split",
                         json={"prompt": chapter["prompt"]})
        assert r.status_code == 200
        rec = _wait_until(client, tid, "splitted", timeout_s=5.0)
        assert rec["split_chunks"] == ["第一章节内容", "第二章节内容", "第三章节内容"]
        # split_<N>.md 落盘
        for n in (1, 2, 3):
            assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"split_{n}.md").exists()

        # 4) 用户编辑后确认
        edited_chunks = ["章节A-编辑版", "章节B-编辑版", "章节C-编辑版"]
        r = client.post(f"/api/tasks/{tid}/confirm-split", json={"chunks": edited_chunks})
        assert r.status_code == 200
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "ready_to_convert"
        assert rec["split_chunks"] == edited_chunks

        # 5) 启动 TTS 转换 → done
        r = client.post(f"/api/tasks/{tid}/convert")
        assert r.status_code == 200
        rec = _wait_until(client, tid, "done", timeout_s=10.0)
        assert rec["status"] == "done"
        assert rec["error"] is None
        assert rec["can_retry"] is False

        # 6) 听文档：v4 用 GET /api/library 列出 status='done' 的任务
        r = client.get("/api/library")
        assert r.status_code == 200
        lib = r.json()
        assert lib["total"] >= 1
        assert any(item["task_id"] == tid for item in lib["items"])

        # 7) 听文档详情：包含 original_md（读自 task_dir/<task_id>.md）+ normalized_md
        r = client.get(f"/api/library/{tid}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["task_id"] == tid
        assert detail["audio_url"] == f"/api/audio/{tid}"
        assert detail["original_md"]
        assert "已标准化" in detail["normalized_md"]
        # lrc/srt 落盘 + URL 可用
        assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"{tid}.mp3").exists()
        assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"{tid}.SRT").exists()
        if detail["lrc_url"]:
            assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"{tid}.LRC").exists()


def test_e2e_skip_normalize_and_skip_split(e2e_env, monkeypatch):
    """跳过路径：upload → skip-normalize → skip-split → convert → done。"""
    app = create_app()
    monkeypatch.setattr(LlmNormalizer, "split_text",
                        AsyncMock(return_value=["ignored"]))  # 不应被调用
    _mock_tts(monkeypatch)

    with TestClient(app) as client:
        # upload
        r = client.post("/api/tasks", files={"file": ("a.md", b"# X\n\nbody", "text/markdown")})
        tid = r.json()["task_id"]
        assert client.get(f"/api/tasks/{tid}").json()["status"] == "draft"

        # skip normalize：复制 <task_id>.md → normalization.md
        r = client.post(f"/api/tasks/{tid}/skip-normalize")
        assert r.status_code == 200
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "ready_to_split"
        assert rec["normalized_text"] is not None
        date_str = rec["date_str"]
        assert (e2e_env["out_dir"] / date_str / tid / "normalization.md").exists()

        # skip split：复制 normalization.md → split_1.md
        r = client.post(f"/api/tasks/{tid}/skip-split")
        assert r.status_code == 200
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "ready_to_convert"
        assert rec["split_chunks"] is not None
        assert len(rec["split_chunks"]) == 1
        assert (e2e_env["out_dir"] / date_str / tid / "split_1.md").exists()

        # convert → done
        r = client.post(f"/api/tasks/{tid}/convert")
        assert r.status_code == 200
        rec = _wait_until(client, tid, "done", timeout_s=10.0)
        assert rec["status"] == "done"
        # task_dir 下 <task_id>.mp3 落盘
        assert (e2e_env["out_dir"] / rec["date_str"] / tid / f"{tid}.mp3").exists()


# ---- v6：标准化 prompt 自定义 + draft 详情返回原文 --------------------------


def test_normalize_presets_endpoint_returns_three(e2e_env):
    """GET /api/normalize-presets 应返回 3 条预设，default 必含。"""
    from app.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/normalize-presets")
        assert r.status_code == 200, r.text
        presets = r.json()
        assert len(presets) == 3
        ids = {p["id"] for p in presets}
        assert ids == {"default", "minimal", "verbatim"}
        # default 的 prompt 应该 == 当前 M3_SYSTEM_PROMPT
        from app.config import M3_SYSTEM_PROMPT, get_m3_system_prompt
        default = next(p for p in presets if p["id"] == "default")
        assert default["prompt"] == get_m3_system_prompt() == M3_SYSTEM_PROMPT
        # minimal / verbatim 有真实 prompt 内容
        for p in presets:
            if p["id"] != "default":
                assert p["prompt"] and len(p["prompt"]) > 50


def test_normalize_endpoint_accepts_custom_prompt(e2e_env, monkeypatch):
    """v6：POST /api/tasks/<id>/normalize 接受 prompt body（直接验证 TaskManager 传参）。"""
    from app.services.task_manager import TaskManager
    from app.services.audio_storage import AudioStorageService, TaskStore, task_date_str
    from app.services.llm_normalizer import LlmNormalizer
    from types import SimpleNamespace

    captured = {}

    async def fake_normalize(self, text, *, system=None):
        captured["system"] = system
        captured["text_len"] = len(text)
        return "已标准化"

    monkeypatch.setattr(LlmNormalizer, "normalize", fake_normalize)

    # 直接 TaskManager 测：拦截 normalize_task 让它不创建异步 task，只记下入参
    out_dir = e2e_env["out_dir"]
    audio = AudioStorageService(out_dir)
    db = out_dir / "lib.db"
    task_store = TaskStore(db)
    pipe = SimpleNamespace(_provider="minimax", _task_id=None)
    # llm mock（normalize_task 内部需要 self._llm 非 None）
    fake_llm = SimpleNamespace()
    mgr = TaskManager(pipeline=pipe, task_store=task_store, audio_storage=audio, llm=fake_llm)

    # 创建草稿任务
    tid = mgr.create_task(b"# x\n\ncontent", filename="x.md")

    seen = {}
    import asyncio as _asyncio

    async def fake_do_normalize(self, task_id, record, system_prompt=None):
        seen["task_id"] = task_id
        seen["system_prompt"] = system_prompt
        return None

    # 拦截 _do_normalize 不真跑 LLM
    monkeypatch.setattr(TaskManager, "_do_normalize", fake_do_normalize)
    # 拦截 asyncio.create_task 让它真同步跑 async 协程
    def sync_coro(coro):
        try:
            _asyncio.get_event_loop().run_until_complete(coro)
        except RuntimeError:
            # 无 loop / 已关闭 -> 直接拿一个新的
            loop = _asyncio.new_event_loop()
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()
        return None
    import app.services.task_manager as _tm
    monkeypatch.setattr(_tm.asyncio, "create_task", lambda c: sync_coro(c))

    # 调 normalize_task 带 prompt
    ok = mgr.normalize_task(tid, system_prompt="CUSTOM_USER_PROMPT")
    assert ok
    assert seen.get("system_prompt") == "CUSTOM_USER_PROMPT"

    # 不带 prompt（默认 None）
    seen.clear()
    # 状态已变 normalizing，需重置
    from app.services.audio_storage import TASK_STATUS_DRAFT
    task_store.update_progress(tid, status=TASK_STATUS_DRAFT, current_stage="draft", progress=0.0, message="reset")
    ok = mgr.normalize_task(tid)
    assert ok
    assert seen.get("system_prompt") is None


def test_normalize_router_accepts_normalize_request_with_prompt(e2e_env):
    """v6：NormalizeRequest 接受 prompt 字段且 router 把它转给 TaskManager。"""
    from app.main import create_app
    from fastapi.testclient import TestClient
    from app.routers.tts import _require_task_manager
    from app.services.task_manager import TaskManager
    from unittest.mock import patch

    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/tasks",
            files={"file": ("demo.md", e2e_env["md_content"], "text/markdown")},
        )
        tid = r.json()["task_id"]

        seen = {}
        with patch.object(TaskManager, "normalize_task",
                          return_value=True) as mock_norm:
            # POST 带 prompt
            r = client.post(
                f"/api/tasks/{tid}/normalize",
                json={"prompt": "MY_PROMPT"},
            )
            assert r.status_code == 200
            args, kwargs = mock_norm.call_args
            # TaskManager.normalize_task(self, task_id, system_prompt=...)
            assert kwargs.get("system_prompt") == "MY_PROMPT" or (
                len(args) >= 3 and args[2] == "MY_PROMPT"
            )


def test_draft_detail_returns_local_clean_text(e2e_env, monkeypatch):
    """v6：详情接口在 draft 状态下应返回 local_clean_text（原文）。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/tasks",
            files={"file": ("demo.md", e2e_env["md_content"], "text/markdown")},
        )
        tid = r.json()["task_id"]

        # draft 状态 → 详情返回 local_clean_text 原文
        r = client.get(f"/api/tasks/{tid}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft"
        assert body.get("local_clean_text") is not None
        assert "Title" in body["local_clean_text"] or "test markdown" in body["local_clean_text"]


def test_non_draft_detail_omits_local_clean_text(e2e_env, monkeypatch):
    """非 draft 状态（如 ready_to_split）详情接口不应回原文。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/tasks",
            files={"file": ("demo.md", e2e_env["md_content"], "text/markdown")},
        )
        tid = r.json()["task_id"]

        # 手动改状态为 ready_to_split（绕过 normalize）
        client.patch is not None  # sanity
        # 直接通过 task_store 改不了（私有路径），改用 skip-normalize 把它推到 ready_to_split
        r = client.post(f"/api/tasks/{tid}/skip-normalize")
        assert r.status_code == 200

        r = client.get(f"/api/tasks/{tid}")
        body = r.json()
        assert body["status"] == "ready_to_split"
        # v6：非 draft 状态，local_clean_text 应为 None（不泄原文）
        assert body.get("local_clean_text") is None
        # 但 local_clean_length 仍提供（仅元数据）
        assert body.get("local_clean_length") is not None


# ---- v6：本地清洗步骤（splitted → local_cleaned → ready_to_convert） -------


def test_clean_options_endpoint_returns_metadata(e2e_env):
    """GET /api/clean-options 应返回 8 条清洗项。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/clean-options")
        assert r.status_code == 200, r.text
        opts = r.json()
        assert len(opts) == 8
        ids = {o["id"] for o in opts}
        assert ids == {
            "url", "email", "code", "emoji",
            "md_symbols", "list_marks", "blockquote", "table_pipe",
        }
        # 每项必须有 label + default
        for o in opts:
            assert "label" in o and isinstance(o["default"], bool)
        # url / email 默认勾选
        defaults = {o["id"] for o in opts if o["default"]}
        assert "url" in defaults and "email" in defaults


def test_local_clean_full_flow(e2e_env, monkeypatch):
    """v6 完整路径：splitted → local-clean → local_cleaned → confirm-split → convert → done。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()

    # mock LLM：让 split_text 返回含 URL / 邮箱 / Markdown 符号的内容
    async def fake_normalize(self, text, *, system=None):
        return text  # 标准化透传

    async def fake_split_text(self, text, *, max_chars=6000, system=None):
        return [
            "详情见 https://example.com 联系 user@test.com",
            "# 标题\n- 列表项 1\n- 列表项 2",
        ]

    monkeypatch.setattr(LlmNormalizer, "normalize", fake_normalize)
    monkeypatch.setattr(LlmNormalizer, "split_text", fake_split_text)
    _mock_tts(monkeypatch)

    with TestClient(app) as client:
        # upload → normalize → split
        r = client.post(
            "/api/tasks",
            files={"file": ("demo.md", b"# Title\n\nbody", "text/markdown")},
        )
        tid = r.json()["task_id"]
        client.post(f"/api/tasks/{tid}/normalize")
        rec = _wait_until(client, tid, "ready_to_split", timeout_s=5.0)
        client.post(f"/api/tasks/{tid}/split", json={"prompt": "按章节划分"})
        rec = _wait_until(client, tid, "splitted", timeout_s=5.0)
        assert rec["split_chunks"] is not None
        date_str = rec["date_str"]
        split_1 = e2e_env["out_dir"] / date_str / tid / "split_1.md"
        split_2 = e2e_env["out_dir"] / date_str / tid / "split_2.md"
        assert split_1.exists() and split_2.exists()
        original_1 = split_1.read_text(encoding="utf-8")
        original_2 = split_2.read_text(encoding="utf-8")
        assert "https://example.com" in original_1
        assert "# 标题" in original_2

        # 触发本地清洗（url + email + md_symbols + list_marks）
        r = client.post(
            f"/api/tasks/{tid}/local-clean",
            json={"options": ["url", "email", "md_symbols", "list_marks"]},
        )
        assert r.status_code == 200
        rec = _wait_until(client, tid, "local_cleaned", timeout_s=5.0)
        # clean_options 回传
        assert rec["clean_options"] == ["url", "email", "md_symbols", "list_marks"]
        # split_chunks 已被清洗
        assert "http" not in rec["split_chunks"][0]
        assert "@" not in rec["split_chunks"][0]
        assert "#" not in rec["split_chunks"][1]
        assert "- " not in rec["split_chunks"][1]
        # 磁盘上 split_<N>.md 也已被覆写
        assert "https://example.com" not in split_1.read_text(encoding="utf-8")
        assert "#" not in split_2.read_text(encoding="utf-8")
        assert "列表项 1" in split_2.read_text(encoding="utf-8")

        # 确认去转换
        r = client.post(f"/api/tasks/{tid}/confirm-split")
        assert r.status_code == 200
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "ready_to_convert"

        # convert → done
        client.post(f"/api/tasks/{tid}/convert")
        rec = _wait_until(client, tid, "done", timeout_s=10.0)
        assert rec["status"] == "done"


def test_skip_local_clean_returns_ready_to_convert(e2e_env, monkeypatch):
    """splitted → skip-local-clean → ready_to_convert（不进 local_cleaned）。"""
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()

    async def fake_normalize(self, text, *, system=None):
        return text

    async def fake_split_text(self, text, *, max_chars=6000, system=None):
        return ["A", "B"]

    monkeypatch.setattr(LlmNormalizer, "normalize", fake_normalize)
    monkeypatch.setattr(LlmNormalizer, "split_text", fake_split_text)
    _mock_tts(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/tasks",
            files={"file": ("demo.md", b"# X", "text/markdown")},
        )
        tid = r.json()["task_id"]
        client.post(f"/api/tasks/{tid}/normalize")
        _wait_until(client, tid, "ready_to_split", timeout_s=5.0)
        client.post(f"/api/tasks/{tid}/split", json={"prompt": "P"})
        _wait_until(client, tid, "splitted", timeout_s=5.0)

        # 跳过清洗
        r = client.post(f"/api/tasks/{tid}/skip-local-clean")
        assert r.status_code == 200
        rec = client.get(f"/api/tasks/{tid}").json()
        assert rec["status"] == "ready_to_convert"
        assert rec["clean_options"] == []
        # split_chunks 不变（仍是 ["A", "B"]）
        assert rec["split_chunks"] == ["A", "B"]