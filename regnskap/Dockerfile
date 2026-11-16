FROM python:3.13-slim AS builder
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
# Distro security fixes land days before the base image is rebuilt (zlib DSA-6613-1, 2026-11-10).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
RUN useradd -m -u 32767 -s /bin/bash appuser && \
    mkdir -p /app/data && chown appuser:appuser /app/data
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src
USER appuser
EXPOSE 8000
# Single process on purpose: download jobs live in memory (src/jobs.py).
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
