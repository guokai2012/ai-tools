# txt2tts Dockerfile
# 多阶段构建：builder 安装 ffmpeg + Python deps，runtime 仅保留必要产物

# ---------- 1. ffmpeg 静态二进制 builder ----------
# 用 jrottenberg/ffmpeg 已是历史；现用 BtbN/ffmpeg 提供静态构建，含 x264/x265 + ffprobe
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg
# 该镜像把 ffmpeg / ffprobe 放在 /usr/local/bin/，直接 COPY 即可


# ---------- 2. Python runtime builder ----------
FROM python:3.12-slim AS python-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# 系统依赖（edge-tts 需要 aiohttp，无需额外；编译某些 wheel 需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---------- 3. Final runtime image ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装 runtime 系统依赖（curl 仅用于 HEALTHCHECK；tzdata 用于正确时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 从 ffmpeg builder 拷贝静态 ffmpeg / ffprobe 到 /usr/local/bin
COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /usr/local/bin/ffprobe /usr/local/bin/ffprobe

# 从 python-builder 拷贝已安装的 Python 包到镜像 site-packages
COPY --from=python-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=python-builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=python-builder /usr/local/bin/uvicorn-django /usr/local/bin/uvicorn-django 2>/dev/null || true

WORKDIR /app

# 复制应用代码
COPY app/ /app/app/
COPY tests/ /app/tests/
COPY pytest.ini /app/
COPY requirements.txt /app/

# 数据卷挂载点（docker-compose.yml 中映射到宿主机）
RUN mkdir -p /app/outputs /app/samples

# 创建非 root 用户运行
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：访问 /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 启动 uvicorn；默认监听 0.0.0.0:8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]