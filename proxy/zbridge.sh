#!/usr/bin/env bash
# Start/stop/check the zbridge GLM proxy for the argos harness.
#
# zbridge exposes an Anthropic-shaped /v1/messages endpoint on the host and
# TRANSLATES it to z.ai's OpenAI-compatible GLM Coding Plan endpoint
# (/api/coding/paas/v4/chat/completions), unlike the plain Anthropic-native
# Run one or the other; both may run side by side on different ports.
#
# Usage:
#   proxy/zbridge.sh check     # print config summary, validate key
#   proxy/zbridge.sh start     # start in background, write PID
#   proxy/zbridge.sh stop      # stop background process (and monitor)
#   proxy/zbridge.sh status    # report bridge + monitor state
#   proxy/zbridge.sh monitor   # start watchdog only (foreground)
#
# Credentials are read from ~/.config/zbridge/env (chmod 600, OUTSIDE the repo):
#   ZB_ZAI_API_KEY=<z.ai coding plan key>
#   ZB_BRIDGE_SECRET=<local shared secret; clients send it as x-api-key>
#   ZB_THINKING_SIG_KEY=<hmac key for thinking-block signatures>
#
# Point the harness at it with .llm_config/glm-zbridge.json, whose "api_key"
# MUST equal ZB_BRIDGE_SECRET (zbridge accepts it via x-api-key).

set -euo pipefail

PROXY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${PROXY_DIR}/.." && pwd)"
if [[ ! -x "${HARNESS_DIR}/.venv/bin/python" && -x "${HARNESS_DIR}/harness/.venv/bin/python" ]]; then
  HARNESS_DIR="${HARNESS_DIR}/harness"
fi
PY="${ZB_PYTHON:-${HARNESS_DIR}/.venv/bin/python}"
# Bind 0.0.0.0 so the agent-server inside the eval Docker container can reach
# the bridge on the host. zbridge's own default is 127.0.0.1, which containers
# CANNOT reach -- overriding it here is required, not cosmetic.
HOST="${ZB_BRIDGE_HOST:-0.0.0.0}"
# 8765 = claude_code_bridge, 8766 = codex_bridge.
# zbridge's upstream default is 8766, which would collide; use 8768.
PORT="${ZB_BRIDGE_PORT:-8768}"
ENV_FILE="${ZB_ENV_FILE:-$HOME/.config/zbridge/env}"

PID_FILE="${PROXY_DIR}/.zbridge.pid"
LOG_FILE="${ZB_BRIDGE_LOG:-${PROXY_DIR}/logs/zbridge.log}"
MONITOR_PID_FILE="${PROXY_DIR}/.zbridge_monitor.pid"
MONITOR_LOG_FILE="${ZB_BRIDGE_MONITOR_LOG:-${PROXY_DIR}/logs/zbridge_monitor.log}"
MONITOR_POLL_SECONDS="${ZB_MONITOR_POLL:-30}"
MONITOR_FAIL_THRESHOLD="${ZB_MONITOR_FAILS:-3}"

if [[ ! -x "$PY" ]]; then
  echo "[zbridge] ERROR: python not found at $PY" >&2
  echo "        Build the harness venv first (cd $HARNESS_DIR && make build)," >&2
  echo "        or set ZB_PYTHON to a python with fastapi+uvicorn+httpx+pydantic." >&2
  exit 1
fi

_load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
  if [[ -z "${ZB_ZAI_API_KEY:-}" ]]; then
    echo "[zbridge] ERROR: ZB_ZAI_API_KEY not set (looked in $ENV_FILE)" >&2
    exit 1
  fi
}

_run_bridge() {
  _load_env
  # PYTHONPATH so the vendored `zbridge` package under proxy/ is importable.
  cd "$PROXY_DIR"
  export PYTHONPATH="${PROXY_DIR}${PYTHONPATH:+:$PYTHONPATH}"
  exec "$PY" -m zbridge --host "$HOST" --port "$PORT"
}

_start_bridge_process() {
  mkdir -p "$(dirname "$LOG_FILE")"
  # setsid detaches from the controlling terminal so the bridge survives the
  # calling shell exiting (plain nohup+& dies when the parent shell is reaped).
  # shellcheck disable=SC2024
  setsid nohup bash "${BASH_SOURCE[0]}" __run >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
}

_wait_for_healthz() {
  local ok=0
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ok" != "1" ]]; then
    echo "[zbridge] FAILED to come up; tail of $LOG_FILE:" >&2
    tail -20 "$LOG_FILE" >&2 || true
    return 1
  fi
  return 0
}

_start_monitor_process() {
  if [[ -f "$MONITOR_PID_FILE" ]] && kill -0 "$(cat "$MONITOR_PID_FILE")" 2>/dev/null; then
    return 0
  fi
  mkdir -p "$(dirname "$MONITOR_LOG_FILE")"
  # shellcheck disable=SC2024
  setsid nohup bash "${BASH_SOURCE[0]}" monitor >>"$MONITOR_LOG_FILE" 2>&1 &
  echo $! >"$MONITOR_PID_FILE"
}

action="${1:-start}"

case "$action" in
  __run)
    _run_bridge
    ;;
  check)
    _load_env
    cd "$PROXY_DIR"
    export PYTHONPATH="${PROXY_DIR}${PYTHONPATH:+:$PYTHONPATH}"
    exec "$PY" -m zbridge --check
    ;;
  start)
    _lock_fd=""
    if command -v flock >/dev/null 2>&1; then
      exec 9>"${PID_FILE}.lock"
      flock 9 || true
      _lock_fd=9
    fi
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[zbridge] already running (PID $(cat "$PID_FILE"))" >&2
    else
      _start_bridge_process
    fi
    if ! _wait_for_healthz; then
      [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
      exit 1
    fi
    [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
    if [[ "${ZB_DISABLE_MONITOR:-0}" != "1" ]]; then
      _start_monitor_process
      echo "[zbridge] monitor up (PID $(cat "$MONITOR_PID_FILE"))" >&2
    fi
    echo "[zbridge] up on http://${HOST}:${PORT} (PID $(cat "$PID_FILE"))" >&2
    echo "[zbridge] generate trajectories with:  --llm-config .llm_config/glm-zbridge.json" >&2
    ;;
  stop)
    if [[ -f "$MONITOR_PID_FILE" ]]; then
      mpid=$(cat "$MONITOR_PID_FILE")
      if kill -0 "$mpid" 2>/dev/null; then
        kill -- -"$mpid" 2>/dev/null || kill "$mpid" 2>/dev/null || true
        sleep 0.3
        kill -9 "$mpid" 2>/dev/null || true
        echo "[zbridge] monitor stopped PID $mpid" >&2
      fi
      rm -f "$MONITOR_PID_FILE"
    fi
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        # setsid makes the child a process-group leader; kill the whole group.
        kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        sleep 0.5
        kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
        echo "[zbridge] stopped PID $pid" >&2
      fi
      rm -f "$PID_FILE"
    else
      echo "[zbridge] no PID file at $PID_FILE" >&2
    fi
    rm -f "${PID_FILE}.lock"
    if command -v lsof >/dev/null 2>&1; then
      for stale in $(lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true); do
        kill "$stale" 2>/dev/null || true
      done
    fi
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[zbridge] running (PID $(cat "$PID_FILE")) on http://${HOST}:${PORT}"
      curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" || true
      echo
      if [[ -f "$MONITOR_PID_FILE" ]] && kill -0 "$(cat "$MONITOR_PID_FILE")" 2>/dev/null; then
        echo "[monitor] running (PID $(cat "$MONITOR_PID_FILE"))"
      else
        echo "[monitor] not running"
      fi
    else
      echo "[zbridge] not running"
      exit 1
    fi
    ;;
  monitor)
    echo "[monitor] started PID=$$ poll=${MONITOR_POLL_SECONDS}s fail_threshold=${MONITOR_FAIL_THRESHOLD}"
    consecutive_failures=0
    while true; do
      if curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
        if [[ "$consecutive_failures" -gt 0 ]]; then
          echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') bridge recovered"
        fi
        consecutive_failures=0
      else
        consecutive_failures=$((consecutive_failures + 1))
        echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') healthz failed (${consecutive_failures}/${MONITOR_FAIL_THRESHOLD})"
        if [[ "$consecutive_failures" -ge "$MONITOR_FAIL_THRESHOLD" ]]; then
          echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') restarting bridge"
          if [[ -f "$PID_FILE" ]]; then
            kill -- -"$(cat "$PID_FILE")" 2>/dev/null || true
            sleep 0.5
            rm -f "$PID_FILE"
          fi
          _start_bridge_process
          consecutive_failures=0
          sleep 2
        fi
      fi
      sleep "$MONITOR_POLL_SECONDS"
    done
    ;;
  *)
    echo "usage: $0 {start|stop|check|status|monitor}" >&2
    exit 2
    ;;
esac
