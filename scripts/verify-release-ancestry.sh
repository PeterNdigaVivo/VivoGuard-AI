#!/usr/bin/env bash
# Keep source builds from dropping released API or scheduler revisions.
set -euo pipefail

release=$(git rev-parse --verify "$1^{commit}")
shift

for service in "$@"; do
  if ! containers=$(docker compose ps --status running -q "$service"); then
    echo "Release preflight failed: cannot list running containers for $service" >&2
    exit 1
  fi
  [ -n "$containers" ] || continue

  while IFS= read -r container; do
    if ! running=$(docker container inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$container"); then
      echo "Release preflight failed: cannot inspect the running revision for $service" >&2
      exit 1
    fi
    [ -n "$running" ] || continue
    if [[ ! "$running" =~ ^[0-9a-f]{40}$ ]]; then
      echo "Release preflight failed: invalid running revision label for $service" >&2
      exit 1
    fi
    if ! git merge-base --is-ancestor "$running" "$release" 2>/dev/null; then
      echo "Release preflight failed: $service revision $running is absent from $release. Fetch and integrate the running release before rebuilding." >&2
      exit 1
    fi
  done <<< "$containers"
done
