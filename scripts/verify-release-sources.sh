#!/usr/bin/env bash
# Read-only check of the running backend entry points against a release commit.
set -euo pipefail

cd "$(dirname "$0")/.."
revision=$(git rev-parse --verify "${1:-HEAD}^{commit}")

verify_source() {
  local service=$1 repo_source=$2 container_source=$3 expected actual containers container
  expected=$(git show "$revision:$repo_source" | sha256sum)
  if ! containers=$(docker compose ps --status running -q "$service") ||
      [ -z "$containers" ]; then
    echo "Release verification failed: no running container for $service" >&2
    return 1
  fi

  while IFS= read -r container; do
    if ! actual=$(docker exec "$container" sh -c 'sha256sum < "$1"' sh "$container_source") ||
        [ "$actual" != "$expected" ]; then
      echo "Release verification failed: $service source mismatch ($container_source)" >&2
      return 1
    fi
  done <<< "$containers"
}

verify_source api backend/app/api/system.py /app/app/api/system.py
verify_source worker-inference backend/app/tasks/inference.py /app/app/tasks/inference.py
verify_source recorder backend/app/tasks/recorder.py /app/app/tasks/recorder.py
verify_source streamer streamer/streamer/main.py /app/streamer/main.py
verify_source streamer backend/app/stream/ffmpeg_worker.py /app/app/stream/ffmpeg_worker.py
verify_source streamer backend/app/stream/http_snapshot_worker.py /app/app/stream/http_snapshot_worker.py

echo "Verified API, inference, recorder and streamer source checks for $revision"
