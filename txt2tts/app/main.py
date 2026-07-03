"""FastAPI application entry point.

Run with:
    D:\\anaconda3\\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

Or simply:
    D:\\anaconda3\\python.exe -m app.main
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env into os.environ BEFORE importing app.config so pydantic-settings
# sees the keys via its env_prefix lookup. We don't use SettingsConfigDict's
# env_file feature because of a clash with env_nested_delimiter.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROVIDER_EDGE, PROVIDER_MIMO, PROVIDERS, get_settings
from app.routers import tts as tts_router
from app.services.audio_storage import (
    AudioStorageService,
    LibraryStore,
    SettingsStore,
    TaskStore,
)
from app.services.edge_tts_provider import EdgeTtsClient
from app.services.llm_normalizer import LlmNormalizer
from app.services.markdown_service import MarkdownService
from app.services.pipeline import TtsPipeline
from app.services.task_watchdog import StallWatchdog
from app.services.tts_client import TtsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting %s on http://%s:%d", settings.app_name, settings.host, settings.port)
    logger.info(
        "TTS: model=%s base=%s key_set=%s",
        settings.tts.model, settings.tts.base_url, bool(settings.tts.api_key),
    )
    logger.info(
        "M3 : model=%s base=%s key_set=%s",
        settings.llm.model, settings.llm.base_url, bool(settings.llm.api_key),
    )

    md_svc = MarkdownService()
    llm_svc = LlmNormalizer(settings.llm)
    tts_client = TtsClient(settings.tts)
    audio_svc = AudioStorageService(Path(settings.output_dir).resolve())
    library_svc = LibraryStore(
        Path(settings.output_dir).resolve() / settings.library_db_filename
    )
    task_svc = TaskStore(
        Path(settings.output_dir).resolve() / settings.library_db_filename
    )
    settings_db = SettingsStore(
        Path(settings.output_dir).resolve() / settings.library_db_filename
    )
    # LyricsService 已移除：edge provider 自身产出 srt/lrc，详情页用
    # ``audio_records.lyrics_path``（旧数据兼容）+ 现有 lrc_parser 即可。

    # 方案二：edge-tts 客户端 + ffmpeg 路径
    ffmpeg_path = Path(settings.edge.ffmpeg_path).resolve()
    ffprobe_path = Path(settings.edge.ffprobe_path).resolve()
    # 容器/Docker 场景：PATH 里能找到 ffmpeg 直接用
    if not ffmpeg_path.exists():
        import shutil
        on_path = shutil.which("ffmpeg")
        if on_path:
            ffmpeg_path = Path(on_path)
        on_path_probe = shutil.which("ffprobe")
        if on_path_probe:
            ffprobe_path = Path(on_path_probe)
    edge_client = EdgeTtsClient(settings.edge) if ffmpeg_path.exists() else None
    if edge_client is None:
        logger.warning("edge-tts provider unavailable: ffmpeg not found at %s", ffmpeg_path)
    else:
        logger.info("edge-tts provider ready (voice=%s, ffmpeg=%s)",
                    settings.edge.default_voice, ffmpeg_path)

    # 当前 provider（DB 优先于 env）
    db_provider = settings_db.get("tts_provider")
    current_provider = db_provider if db_provider in PROVIDERS else settings.tts_provider
    if db_provider is None:
        settings_db.set("tts_provider", current_provider)
    logger.info("active TTS provider: %s", current_provider)

    def build_pipeline(provider: str) -> TtsPipeline:
        return TtsPipeline(
            markdown=md_svc,
            llm=llm_svc,
            tts=tts_client,
            audio=audio_svc,
            library=library_svc,
            edge_tts=edge_client,
            ffmpeg_path=ffmpeg_path if ffmpeg_path.exists() else None,
            ffprobe_path=ffprobe_path if ffprobe_path.exists() else None,
            provider=provider,
            edge_settings=settings.edge,
        )

    pipelines = {p: build_pipeline(p) for p in PROVIDERS}

    tts_router.configure(
        markdown=md_svc,
        llm=llm_svc,
        tts=tts_client,
        audio=audio_svc,
        library=library_svc,
        task_store=task_svc,
        settings_store=settings_db,
        pipelines=pipelines,
        active_provider=current_provider,
    )

    app.state.markdown = md_svc
    app.state.llm = llm_svc
    app.state.tts = tts_client
    app.state.audio = audio_svc
    app.state.library = library_svc
    app.state.task_store = task_svc
    app.state.settings_store = settings_db
    app.state.edge_tts = edge_client
    app.state.ffmpeg_path = ffmpeg_path
    app.state.ffprobe_path = ffprobe_path
    app.state.pipelines = pipelines
    app.state.active_provider = current_provider

    # 后台任务看门狗：扫描 status='processing' 且 updated_at 超过
    # task_stall_timeout_sec 没动过的任务，自动标 failed_retryable。
    watchdog = StallWatchdog(
        task_store=task_svc,
        threshold_sec=settings.task_stall_timeout_sec,
        interval_sec=settings.task_watchdog_interval_sec,
        enabled=settings.task_watchdog_enabled,
    )
    watchdog.start()
    app.state.watchdog = watchdog

    try:
        yield
    finally:
        await watchdog.stop()
        await llm_svc.aclose()
        await tts_client.aclose()
        if edge_client is not None:
            await edge_client.aclose()
        logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="txt2tts",
        description="Read any .md aloud via MiniMax M3 (normalize) + MiniMax Speech 2.8 (TTS).",
        version="0.2.0",
        lifespan=lifespan,
    )

    static_dir = Path(settings.static_dir).resolve()
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(tts_router.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/static/index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        ico = static_dir / "favicon.ico"
        if ico.exists():
            return FileResponse(ico)
        return Response(status_code=204)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=s.reload,
        log_level="info",
    )