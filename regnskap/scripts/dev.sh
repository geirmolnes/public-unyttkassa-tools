#!/bin/sh
# Run the app locally without a login service: http://127.0.0.1:8000, or the port in $PORT.
# DEV_INSECURE_AUTH=1 lets every request in as dev@localhost, who is also an administrator.
# Local only: the app refuses to start with it when DOKKU_APP_TYPE is set (in production).
cd "$(dirname "$0")/.." || exit 1
export DEV_INSECURE_AUTH=1
exec uv run uvicorn src.main:app --reload --host 127.0.0.1 --port "${PORT:-8000}"
