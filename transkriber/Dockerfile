# Replaced the herokuish buildpacks on 2026-11-10: its Ubuntu stack image lagged Ubuntu's security fixes (5 highs,
# no newer stack to pull), and the ffmpeg buildpack fetched a nightly build. Layout, uid and port match the old image:
# /app, uid 32767 (owns the storage mounts), Procfile's uvicorn on $PORT (5000, no EXPOSE).
FROM python:3.12-slim AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
# Distro security fixes land days before the base image is rebuilt (zlib DSA-6613-1, 2026-11-10).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
# ffmpeg/ffprobe: duration, bitrate and clips (src/transcribe.py, src/main.py).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
RUN useradd -m -u 32767 -s /bin/bash appuser && \
    mkdir -p /app/uploads /app/ai-usage && chown -R appuser:appuser /app
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src
COPY --chown=appuser:appuser Procfile app.json ./
USER appuser
# Procfile's web line wins; this is the fallback.
CMD ["sh", "-c", "exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-5000}"]
