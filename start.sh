#!/usr/bin/env bash
set -euo pipefail
exec /home/ubuntu/agent-hub/.venv/bin/uvicorn app:app --host "${AGENT_HUB_HOST:-127.0.0.1}" --port "${AGENT_HUB_PORT:-8090}"
