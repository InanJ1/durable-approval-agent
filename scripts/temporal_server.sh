#!/usr/bin/env bash
# Start or stop the Temporal dev server in Docker (official CLI image, pinned).
#   scripts/temporal_server.sh start | stop | status
# gRPC on localhost:7233, Web UI on http://localhost:8233.
# History is kept in a named volume, so it survives a container restart.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # stop Git Bash rewriting /data into a Windows path

NAME=durable-approval-agent-temporal
IMAGE=temporalio/temporal:1.9.1
VOLUME=durable-approval-agent-temporal-data

case "${1:-start}" in
  start)
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "$NAME already running"; exit 0
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume create "$VOLUME" >/dev/null
    # The image runs as user "temporal"; make the volume writable for it once.
    docker run --rm --user root -v "$VOLUME":/data --entrypoint sh "$IMAGE" -c "chown temporal /data" >/dev/null
    docker run -d --name "$NAME" -p 7233:7233 -p 8233:8233 -v "$VOLUME":/data "$IMAGE" \
      server start-dev --ip 0.0.0.0 --db-filename /data/temporal.db >/dev/null
    for _ in $(seq 1 30); do
      if docker exec "$NAME" temporal operator cluster health 2>/dev/null | grep -q SERVING; then
        echo "$NAME is up ($IMAGE): gRPC localhost:7233, UI http://localhost:8233"; exit 0
      fi
      sleep 1
    done
    echo "$NAME did not report healthy in 30 s"; docker logs --tail 20 "$NAME"; exit 1
    ;;
  stop)
    docker stop "$NAME" >/dev/null 2>&1 && docker rm "$NAME" >/dev/null && echo "stopped $NAME" || echo "$NAME not running"
    ;;
  status)
    docker ps --filter "name=$NAME" --format '{{.Names}} {{.Image}} {{.Status}}'
    ;;
  *)
    echo "usage: $0 start|stop|status"; exit 2
    ;;
esac
