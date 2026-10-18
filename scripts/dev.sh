#!/usr/bin/env bash
# Start or stop the local processes (Git Bash on Windows):
#   scripts/dev.sh start [worker|agent|ui ...]   default: all three
#   scripts/dev.sh stop
# worker = Temporal worker, agent = langgraph dev on :2024, ui = agent-chat-ui on :3000.
# Logs go to var/<name>.log; Windows PIDs to var/<name>.pid so stop kills only these trees.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var
PY=.venv/Scripts/python

launch() {  # name, command...
  local name=$1; shift
  "$@" > "var/$name.log" 2>&1 &
  cat "/proc/$!/winpid" > "var/$name.pid"
  echo "started $name (pid $(cat "var/$name.pid"), log var/$name.log)"
}

wait_http() {  # url, seconds
  for _ in $(seq 1 "$2"); do curl -s -m 2 -o /dev/null "$1" && return 0; sleep 1; done
  echo "timed out waiting for $1"; return 1
}

case "${1:-start}" in
  start)
    shift || true
    targets=${*:-worker agent ui}
    for t in $targets; do
      case $t in
        worker) launch worker "$PY" -m approval.worker ;;
        agent)  launch agent .venv/Scripts/langgraph dev --no-browser --no-reload --port 2024
                wait_http http://localhost:2024/ok 90 ;;
        ui)     (cd ui && [ -f .env ] || cp .env.example .env)
                launch ui bash -c "cd ui && npm run dev"
                wait_http http://localhost:3000 180 ;;
      esac
    done
    ;;
  stop)
    for f in var/*.pid; do
      [ -e "$f" ] || continue
      taskkill //F //T //PID "$(cat "$f")" >/dev/null 2>&1 && echo "stopped $(basename "$f" .pid)" || true
      rm -f "$f"
    done
    ;;
  *) echo "usage: $0 start [worker|agent|ui] | stop"; exit 2 ;;
esac
