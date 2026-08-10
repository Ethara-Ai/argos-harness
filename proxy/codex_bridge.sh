#!/usr/bin/env bash
# Start/stop/check the Codex (ChatGPT-subscription) OAuth bridge for aurora.
#
# The bridge exposes an OpenAI-Responses endpoint on the host that injects your
# Codex OAuth subscription token (from ~/.codex/auth.json), so trajectory
# generation runs on the ChatGPT subscription instead of an OPENAI_API_KEY.
# Point the harness at it with an LLM config whose "base_url" is the bridge
# (.llm_config/codex.json) and whose "model" is "openai/<codex-model>".
#
# Sibling of claude_code_bridge.sh; runs on port 8766 (8765 stays Claude), so
# both bridges can run side by side for mixed trajectory/rubric provider setups.
#
# Usage (run from anywhere; paths resolve relative to this script):
#   proxy/codex_bridge.sh check     # validate credentials only
#   proxy/codex_bridge.sh start     # start in background, write PID
#   proxy/codex_bridge.sh stop      # stop background process (and monitor)
#   proxy/codex_bridge.sh status    # report bridge + monitor state
#   proxy/codex_bridge.sh monitor   # start watchdog only (foreground)

set -euo pipefail

PROXY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${PROXY_DIR}/.." && pwd)"
if [[ ! -x "${HARNESS_DIR}/.venv/bin/python" && -x "${HARNESS_DIR}/harness/.venv/bin/python" ]]; then
  HARNESS_DIR="${HARNESS_DIR}/harness"
fi
PY="${AURORA_CODEX_PYTHON:-${HARNESS_DIR}/.venv/bin/python}"
# Bind 0.0.0.0 so the agent-server inside the eval Docker container can reach
# the bridge on the host (docker0 gateway 172.17.0.1 on Linux/EC2, or
# host.docker.internal on macOS Docker Desktop).
HOST="${AURORA_CODEX_BRIDGE_HOST:-0.0.0.0}"
PORT="${AURORA_CODEX_BRIDGE_PORT:-8766}"
PID_FILE="${PROXY_DIR}/.codex_bridge.pid"
LOG_FILE="${AURORA_CODEX_BRIDGE_LOG:-${PROXY_DIR}/logs/codex_bridge.log}"
MONITOR_PID_FILE="${PROXY_DIR}/.codex_bridge_monitor.pid"
MONITOR_LOG_FILE="${AURORA_CODEX_BRIDGE_MONITOR_LOG:-${PROXY_DIR}/logs/codex_bridge_monitor.log}"
MONITOR_POLL_SECONDS="${AURORA_CODEX_MONITOR_POLL:-30}"
MONITOR_FAIL_THRESHOLD="${AURORA_CODEX_MONITOR_FAILS:-3}"

if [[ ! -x "$PY" ]]; then
  echo "[codex-bridge] ERROR: python not found at $PY" >&2
  echo "         Build the harness venv first (cd $HARNESS_DIR && make build)," >&2
  echo "         or set AURORA_CODEX_PYTHON to a python with fastapi+uvicorn+httpx." >&2
  exit 1
fi

_run_bridge() {
  cd "$PROXY_DIR"
  exec "$PY" -m codex_bridge --host "$HOST" --port "$PORT"
}

_start_bridge_process() {
  mkdir -p "$(dirname "$LOG_FILE")"
  # shellcheck disable=SC2024
  nohup bash "${BASH_SOURCE[0]}" __run >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
}

_wait_for_healthz() {
  local ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ok" != "1" ]]; then
    echo "[codex-bridge] FAILED to come up; tail of $LOG_FILE:" >&2
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
  nohup bash "${BASH_SOURCE[0]}" monitor \
    >>"$MONITOR_LOG_FILE" 2>&1 &
  echo $! >"$MONITOR_PID_FILE"
}


action="${1:-start}"

case "$action" in
  __run)
    _run_bridge
    ;;
  check)
    cd "$PROXY_DIR"
    exec "$PY" -m codex_bridge --check
    ;;
  start)
    _lock_fd=""
    if command -v flock >/dev/null 2>&1; then
      exec 9>"${PID_FILE}.lock"
      flock 9 || true
      _lock_fd=9
    fi
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[codex-bridge] already running (PID $(cat "$PID_FILE"))" >&2
    else
      _start_bridge_process
    fi
    if ! _wait_for_healthz; then
      [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
      exit 1
    fi
    [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
    if [[ "${AURORA_CODEX_DISABLE_MONITOR:-0}" != "1" ]]; then
      _start_monitor_process
      echo "[codex-bridge] monitor up (PID $(cat "$MONITOR_PID_FILE"))" >&2
    fi
    echo "[codex-bridge] up on http://${HOST}:${PORT} (PID $(cat "$PID_FILE"))" >&2
    echo "[codex-bridge] generate trajectories with:  --llm-config .llm_config/codex.json" >&2
    echo "         (set that config's base_url to the host address your container can reach:" >&2
    echo "          172.17.0.1:${PORT} on Linux/EC2, host.docker.internal:${PORT} on macOS)" >&2
    ;;
  stop)
    if [[ -f "$MONITOR_PID_FILE" ]]; then
      mpid=$(cat "$MONITOR_PID_FILE")
      if kill -0 "$mpid" 2>/dev/null; then
        kill "$mpid" || true
        sleep 0.3
        kill -0 "$mpid" 2>/dev/null && kill -9 "$mpid" 2>/dev/null || true
        echo "[codex-bridge] monitor stopped PID $mpid" >&2
      fi
      rm -f "$MONITOR_PID_FILE"
    fi
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" || true
        sleep 0.5
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        echo "[codex-bridge] stopped PID $pid" >&2
      fi
      rm -f "$PID_FILE"
    else
      echo "[codex-bridge] no PID file at $PID_FILE" >&2
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
      echo "[codex-bridge] running (PID $(cat "$PID_FILE")) on http://${HOST}:${PORT}"
      curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/healthz" || true
      echo
      if [[ -f "$MONITOR_PID_FILE" ]] && kill -0 "$(cat "$MONITOR_PID_FILE")" 2>/dev/null; then
        echo "[monitor] running (PID $(cat "$MONITOR_PID_FILE"))"
      else
        echo "[monitor] not running"
      fi
    else
      echo "[codex-bridge] not running"
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
            kill "$(cat "$PID_FILE")" 2>/dev/null || true
            sleep 0.5
            kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
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
