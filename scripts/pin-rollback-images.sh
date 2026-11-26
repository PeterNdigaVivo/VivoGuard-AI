#!/usr/bin/env bash
# Preserve the exact images used by running Compose services before a build.
set -euo pipefail

cd "$(dirname "$0")/.."

for service in "$@"; do
  if ! containers=$(docker compose ps --status running -q "$service"); then
    echo "Rollback preflight failed: cannot list running containers for $service" >&2
    exit 1
  fi
  [ -n "$containers" ] || continue

  while IFS= read -r container; do
    if ! image_id=$(docker container inspect --format '{{.Image}}' "$container") ||
        [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "Rollback preflight failed: cannot identify the running image for $service" >&2
      exit 1
    fi
    if ! available_id=$(docker image inspect --format '{{.Id}}' "$image_id" 2>/dev/null) ||
        [ "$available_id" != "$image_id" ]; then
      echo "Rollback preflight failed: $service image $image_id is unavailable locally" >&2
      exit 1
    fi
    rollback_ref="vivoguard/rollback:$service-${image_id#sha256:}"
    if ! docker image tag "$image_id" "$rollback_ref" ||
        [ "$(docker image inspect --format '{{.Id}}' "$rollback_ref")" != "$image_id" ]; then
      echo "Rollback preflight failed: could not preserve $service image $image_id" >&2
      exit 1
    fi
    echo "Preserved $service rollback image $rollback_ref"
  done <<< "$containers"
done
