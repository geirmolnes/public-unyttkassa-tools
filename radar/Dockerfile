FROM python:3.13-slim AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
# The built-in RSS reader (radar-fetch). Build with `--build-arg RADAR_EXTRAS=""` to leave
# it out when a scraper of your own fills the article database.
ARG RADAR_EXTRAS="--extra rss"
RUN uv sync --frozen --no-install-project --no-dev $RADAR_EXTRAS
COPY src ./src
RUN uv sync --frozen --no-dev $RADAR_EXTRAS

FROM python:3.13-slim
# Distro security fixes land days before the base image is rebuilt (zlib DSA-6613-1, 2026-11-10).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    RADAR_DATA_DIR=/app/data \
    SCRAPER_DB_PATH=/app/data/news_scraper.db \
    ARCHIVE_DB_PATH=/app/archive-data/unytt_artikler.db
RUN useradd -m -u 32767 -s /bin/bash appuser && \
    mkdir -p /app/data /app/news-data /app/archive-data && chown appuser:appuser /app/data
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src
# The feed list radar-fetch reads: feeds.txt if the repo has one, else only the example.
COPY --chown=appuser:appuser feeds*.txt ./
USER appuser
EXPOSE 8000
# --proxy-headers: behind Dokku's nginx the app must trust X-Forwarded-Proto,
# or every generated absolute URL comes out http:// on an https page.
CMD ["uvicorn", "radar.web.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
