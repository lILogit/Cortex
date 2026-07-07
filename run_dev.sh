#!/usr/bin/env bash
# Dev launcher: uvicorn (reload) + ngrok tunnel for the Telegram webhook.
# Reads PUBLIC_BASE_URL from .env (or CORTEX_ENV_FILE if set) to derive the
# ngrok domain — keep that value in sync with your reserved ngrok domain.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

ENV_FILE="${CORTEX_ENV_FILE:-.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found — cp .env.example $ENV_FILE first" >&2
  exit 1
fi

PUBLIC_BASE_URL="$(grep -m1 '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
DOMAIN="${PUBLIC_BASE_URL#https://}"

if [ -z "$DOMAIN" ]; then
  echo "warning: PUBLIC_BASE_URL not set in $ENV_FILE — skipping ngrok, Telegram webhook won't be reachable" >&2
else
  echo "starting ngrok tunnel: https://$DOMAIN -> localhost:8000"
  ngrok http --domain="$DOMAIN" 8000 > /tmp/cortex_ngrok.log 2>&1 &
  NGROK_PID=$!
  trap 'kill "$NGROK_PID" 2>/dev/null' EXIT
fi

echo "starting uvicorn on :8000 (env: $ENV_FILE)"
CORTEX_ENV_FILE="$ENV_FILE" .venv/bin/uvicorn app.main:app --reload --port 8000
