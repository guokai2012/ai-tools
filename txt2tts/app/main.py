"""FastAPI application entry point (v4: task_id 一统天下；彻底 greenfield).

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

from app.config import PROVIDER_EDGE, PROVIDER_MINIMAX, PROVIDERS, get_settings
from app.routers import tts as tts_router
from app.services.audio_storage import (
    AudioStorageService,
    SettingsStore,
    TaskStore,
)
from app.services.edge_tts_provider import EdgeTtsClient
from app.services.llm_normalizer import LlmNormalizer
from app.services.minimax_tts_provider import MinimaxTtsClient
from app.services.pipeline import TtsPipeline
from app.services.task_watchdog import StallWatchdog

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
        settings.minimax.model, settings.minimax.base_url,
        bool(settings.minimax.api_key or settings.llm.api_key),
    )
    logger.info(
        "M3 : model=%s base=%s key_set=%s",
        settings.llm.model, settings.llm.base_url, bool(settings.llm.api_key),
    )

    md_svc = None  # v5 起移除本地 Markdown 清洗：原始 MD 直接送 M3
    llm_svc = LlmNormalizer(settings.llm)
    # 默认方案：MiniMax speech-2.8-hd。api_key 留空时回落 LLM__API_KEY（同平台共用）
    minimax_client = MinimaxTtsClient(
        settings.minimax,
        api_key_fallback=settings.llm.api_key,
    )
    audio_svc = AudioStorageService(Path(settings.output_dir).resolve())
    # tasks 表与 app_settings 表共享同一个 SQLite 文件（不再有 audio_records 表）
    db_path = Path(settings.output_dir).resolve() / settings.library_db_filename
    task_svc = TaskStore(db_path)
    settings_db = SettingsStore(db_path)

    # 备选 edge-tts：需要本地 ffmpeg
    ffmpeg_path = Path(settings.edge.ffmpeg_path).resolve()
    ffprobe_path = Path(settings.edge.ffprobe_path).resolve()
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
            llm=llm_svc,
            audio=audio_svc,
            minimax_tts=minimax_client,
            edge_tts=edge_client,
            ffmpeg_path=ffmpeg_path if ffmpeg_path.exists() else None,
            ffprobe_path=ffprobe_path if ffprobe_path.exists() else None,
            provider=provider,
            edge_settings=settings.edge,
        )

    pipelines = {p: build_pipeline(p) for p in PROVIDERS}

    tts_router.configure(
        llm=llm_svc,
        audio=audio_svc,
        task_store=task_svc,
        settings_store=settings_db,
        pipelines=pipelines,
        active_provider=current_provider,
    )

    app.state.llm = llm_svc
    app.state.minimax_tts = minimax_client
    app.state.audio = audio_svc
    app.state.task_store = task_svc
    app.state.settings_store = settings_db
    app.state.edge_tts = edge_client
    app.state.ffmpeg_path = ffmpeg_path
    app.state.ffprobe_path = ffprobe_path
    app.state.pipelines = pipelines
    app.state.active_provider = current_provider

    # 后台任务看门狗：扫描 status='converting' 且 updated_at 超过
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
        await minimax_client.aclose()
        if edge_client is not None:
            await edge_client.aclose()
        logger.info("Shutdown complete.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="txt2tts",
        description=(
            "Read any .md aloud via MiniMax M3 (normalize) + "
            "MiniMax speech-2.8-hd (TTS, supports native SRT subtitles)."
        ),
        version="0.4.0",
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