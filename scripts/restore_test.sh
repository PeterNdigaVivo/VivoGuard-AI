#!/usr/bin/env bash
# VivoGuard — prove a backup really restores, WITHOUT touching live.
#
#   sudo scripts/restore_test.sh               test the newest daily backup
#   sudo scripts/restore_test.sh FILE.dump     test a specific backup
#
# Starts a throwaway Postgres container with NO network (nothing can reach
# it and it can reach nothing — in particular not the live database),
# restores the dump into it, and compares the core-table row counts with
# the .counts file written at backup time. Prints PASS or FAIL and always
# deletes the throwaway container. The live system is never contacted.
set -Eeuo pipefail

CONFIG_FILE="${VG_BACKUP_CONFIG:-/etc/vivoguard/backup.env}"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONFIG_FILE"
fi
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vivoguard}"
PG_CONTAINER="${PG_CONTAINER:-vivoguard-postgres}"

log()  { echo "[restore-test] $*"; }
fail() { log "FAIL: $*" >&2; exit 1; }

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    DUMP=$(find "$BACKUP_DIR/daily" -maxdepth 1 -name 'vivoguard-*.dump' 2>/dev/null \
           | sort | tail -n 1)
    [ -n "$DUMP" ] || fail "no backups found in $BACKUP_DIR/daily"
fi
[ -s "$DUMP" ] || fail "$DUMP is missing or empty"
COUNTS="${DUMP%.dump}.counts"
[ -s "$COUNTS" ] || fail "row-count file $COUNTS is missing"

SUMS="${DUMP%.dump}.sha256"
if [ -f "$SUMS" ]; then
    ( cd "$(dirname "$DUMP")" && sha256sum --quiet -c "$(basename "$SUMS")" ) \
        || fail "checksum mismatch — the backup file is damaged"
fi

# Same Postgres version as live when we can tell, else the compose default.
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$PG_CONTAINER" 2>/dev/null || true)
IMAGE="${IMAGE:-postgres:16-alpine}"
NAME="vivoguard-restore-test-$$"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

log "testing $DUMP"
log "starting throwaway database ($IMAGE, no network)"
# trust auth is safe here: --network none means nothing can connect.
docker run -d --rm --name "$NAME" --network none \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_USER=restore -e POSTGRES_DB=restore_test \
    "$IMAGE" > /dev/null || fail "could not start the throwaway container"

ready=""
for _ in $(seq 1 60); do
    # A second check guards against the image's init-time restart.
    if docker exec "$NAME" pg_isready -U restore -d restore_test > /dev/null 2>&1; then
        sleep 2
        if docker exec "$NAME" pg_isready -U restore -d restore_test > /dev/null 2>&1; then
            ready=1; break
        fi
    fi
    sleep 1
done
[ -n "$ready" ] || fail "throwaway database did not start"

log "restoring (this can take a few minutes)"
docker exec -i "$NAME" pg_restore -U restore -d restore_test \
    --no-owner --no-privileges --exit-on-error < "$DUMP" \
    || fail "pg_restore reported errors"

status=0
printf '%-18s %12s %12s\n' "table" "at backup" "restored"
while read -r table expected; do
    [ -n "$table" ] || continue
    got=$(docker exec "$NAME" psql -U restore -d restore_test -At \
            -c "SELECT count(*) FROM \"$table\"") || got="error"
    printf '%-18s %12s %12s\n' "$table" "$expected" "$got"
    # Counts are taken seconds before the dump, so rows added in between
    # can only make the restored count larger, never smaller.
    if [ "$got" = "error" ] || [ "$got" -lt "$expected" ]; then
        status=1
    fi
done < "$COUNTS"

version=$(docker exec "$NAME" psql -U restore -d restore_test -At \
            -c "SELECT version_num FROM alembic_version" 2>/dev/null || echo "unknown")
log "schema version in backup: $version"

[ "$status" -eq 0 ] || fail "restored row counts are lower than at backup time"
log "PASS — $DUMP restores correctly"
