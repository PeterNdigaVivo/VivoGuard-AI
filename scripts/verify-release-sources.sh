#!/usr/bin/env bash
# Read-only check of the running backend entry points against a release commit.
set -euo pipefail

cd "$(dirname "$0")/.."
revision=$(git rev-parse --verify "${1:-HEAD}^{commit}")

verify_source() {
  local service=$1 source=$2 expected actual containers container
  expected=$(git show "$revision:backend/$source" | sha256sum)
  if ! containers=$(docker compose ps --status running -q "$service") ||
      [ -z "$containers" ]; then
    echo "Release verification failed: no running container for $service" >&2
    return 1
  fi

  while IFS= read -r container; do
    if ! actual=$(docker exec "$container" sh -c 'sha256sum < "$1"' sh "/app/$source") ||
        [ "$actual" != "$expected" ]; then
      echo "Release verification failed: $service source mismatch ($source)" >&2
      return 1
    fi
  done <<< "$containers"
}

verify_source api app/api/system.py
verify_source worker-inference app/tasks/inference.py
verify_source recorder app/tasks/recorder.py

echo "Verified API, inference and recorder source checks for $revision"
