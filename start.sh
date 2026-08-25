#!/usr/bin/env bash
# Entrypoint for agent-hub.service.
#
# `app.main:app` is explicit on purpose: `app` is a package now, and a bare
# `app:app` would resolve to its __init__ and fail with a confusing error rather
# than starting the service.
#
# --timeout-graceful-shutdown bounds uvicorn's own wait for in-flight work. The
# engine parks running jobs within AGENT_HUB_SHUTDOWN_GRACE (default 10s), so 20s
# leaves room for that and still lands inside systemd's TimeoutStopSec. v1 bounded
# neither: every stop waited 90s and escalated to SIGKILL.
#
# Access logging is off because the app emits structured JSON logs of its own;
# uvicorn's line-per-request would double every entry in the journal.
set -euo pipefail

exec /home/ubuntu/agent-hub/.venv/bin/uvicorn app.main:app \
  --host "${AGENT_HUB_HOST:-127.0.0.1}" \
  --port "${AGENT_HUB_PORT:-8090}" \
  --timeout-graceful-shutdown 20 \
  --no-access-log
