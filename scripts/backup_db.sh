#!/usr/bin/env bash
# VivoGuard — nightly database backup.
#
#   scripts/backup_db.sh                 take a backup now
#   scripts/backup_db.sh --prune DIR N   keep only the newest N backup sets
#                                        in DIR (used by the tests)
#
# What it does:
#   1. Checks there is enough free disk and that Postgres is running.
#   2. Records row counts of the core tables (used by restore_test.sh).
#   3. Dumps the whole database (pg_dump custom format, compressed) and
#      checks the dump is readable before keeping it.
#   4. Sundays: also keeps the copy as a weekly backup.
#   5. Optionally archives extra data folders and copies everything
#      off the server with rclone.
#   6. Deletes old copies (default: 7 daily + 4 weekly).
#
# No secrets: the dump runs INSIDE the Postgres container with that
# container's own POSTGRES_USER / POSTGRES_DB and local socket login, so
# this script never reads .env or handles a password. Backups contain
# the database only — never .env. Keep a copy of .env (above all
# CREDENTIALS_FERNET_KEY) in a password manager; see
# docs/BACKUP_AND_RESTORE.md.
#
# Settings come from /etc/vivoguard/backup.env (not in git); see
# deploy/backup/backup.env.example. All have safe defaults.
set -Eeuo pipefail

CONFIG_FILE="${VG_BACKUP_CONFIG:-/etc/vivoguard/backup.env}"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONFIG_FILE"
fi
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vivoguard}"
PG_CONTAINER="${PG_CONTAINER:-vivoguard-postgres}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"
INCLUDE_DATA_DIRS="${INCLUDE_DATA_DIRS:-}"   # space-separated paths, empty = off
OFFSITE_REMOTE="${OFFSITE_REMOTE:-}"         # rclone "remote:path", empty = off
OFFSITE_KEEP_DAYS="${OFFSITE_KEEP_DAYS:-35}"

# Core tables whose row counts prove a restore worked.
COUNT_TABLES="users stores cameras alerts detection_events"

log()  { echo "[vivoguard-backup] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail() { log "BACKUP FAILED: $*" >&2; exit 1; }

# Keep only the newest $2 backup sets in directory $1. A set is every file
# sharing one "vivoguard-<stamp>" prefix (.dump, .counts, .sha256,
# -data.tar.gz). Stamps sort chronologically, so newest = last.
prune_sets() {
    local dir="$1" keep="$2" stamp
    [ -d "$dir" ] || return 0
    case "$keep" in ''|*[!0-9]*) fail "keep count '$keep' is not a number" ;; esac
    [ "$keep" -ge 1 ] || fail "keep count must be at least 1"
    find "$dir" -maxdepth 1 -type f -name 'vivoguard-*.dump' -printf '%f\n' \
        | sed -E 's/^vivoguard-(.*)\.dump$/\1/' | sort | head -n "-$keep" \
        | while read -r stamp; do
            log "removing old backup set $stamp from $dir"
            find "$dir" -maxdepth 1 -type f -name "vivoguard-${stamp}*" -delete
        done
}

if [ "${1:-}" = "--prune" ]; then
    [ $# -eq 3 ] || fail "usage: $0 --prune DIR KEEP"
    prune_sets "$2" "$3"
    exit 0
fi

TMP=""
BASE=""
DUMP_OK=""
# On failure before the dump is safely kept, remove every file of this
# attempt so no half-made backup set is left behind. Once the dump is
# kept, a later failure (e.g. the off-site copy) leaves it in place.
cleanup() {
    [ -n "$TMP" ] && rm -f "$TMP"
    if [ -z "$DUMP_OK" ] && [ -n "$BASE" ]; then
        rm -f "$BASE.counts" "$BASE.sha256" "$BASE-data.tar.gz"
    fi
    return 0
}
trap cleanup EXIT
trap 'fail "unexpected error at line $LINENO"' ERR

umask 077
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# One run at a time (a slow dump must not overlap the next timer).
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || fail "another backup is still running"

free_mb=$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')
[ "$free_mb" -ge "$MIN_FREE_MB" ] \
    || fail "only ${free_mb} MB free in $BACKUP_DIR (need ${MIN_FREE_MB} MB)"

[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = "true" ] \
    || fail "container $PG_CONTAINER is not running"

pg() { docker exec -i "$PG_CONTAINER" sh -c "$1"; }

STAMP=$(date '+%Y-%m-%dT%H%M%S')
BASE="$BACKUP_DIR/daily/vivoguard-$STAMP"

# Row counts, taken just before the dump (restore_test.sh compares them).
query=""
for t in $COUNT_TABLES; do
    query="${query:+$query UNION ALL }SELECT '$t', count(*) FROM $t"
done
pg "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -v ON_ERROR_STOP=1 -At -F ' ' -c \"$query\"" \
    > "$BASE.counts" || fail "could not count rows"

log "dumping database from $PG_CONTAINER"
TMP="$BASE.dump.partial"
# shellcheck disable=SC2016  # $POSTGRES_* expand inside the container
pg 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z 6' > "$TMP" \
    || fail "pg_dump failed"
[ -s "$TMP" ] || fail "pg_dump produced an empty file"
pg 'pg_restore --list' < "$TMP" > /dev/null || fail "dump is not readable"
mv "$TMP" "$BASE.dump"
TMP=""
DUMP_OK=1
( cd "$BACKUP_DIR/daily" && sha256sum "vivoguard-$STAMP.dump" > "vivoguard-$STAMP.sha256" )
log "database dump OK: $BASE.dump ($(du -h "$BASE.dump" | cut -f1))"

if [ -n "$INCLUDE_DATA_DIRS" ]; then
    log "archiving data folders: $INCLUDE_DATA_DIRS"
    # shellcheck disable=SC2086  # intentional word-splitting of the list
    tar -czf "$BASE-data.tar.gz" $INCLUDE_DATA_DIRS || fail "data archive failed"
fi

# Sunday copy becomes the weekly backup (hard links: no extra space).
if [ "$(date +%u)" = "7" ]; then
    for f in "$BACKUP_DIR/daily/vivoguard-$STAMP"*; do
        ln -f "$f" "$BACKUP_DIR/weekly/"
    done
    log "kept as weekly backup"
fi

prune_sets "$BACKUP_DIR/daily" "$KEEP_DAILY"
prune_sets "$BACKUP_DIR/weekly" "$KEEP_WEEKLY"

if [ -n "$OFFSITE_REMOTE" ]; then
    command -v rclone > /dev/null || fail "OFFSITE_REMOTE is set but rclone is not installed"
    log "copying to off-site storage"
    # `copy`, never `sync`: an empty or replaced local disk must not be
    # able to delete the off-site copies. Old off-site files age out.
    rclone copy "$BACKUP_DIR" "$OFFSITE_REMOTE" --exclude '.lock' --exclude '*.partial' \
        || fail "off-site copy failed (local backup is fine)"
    rclone delete "$OFFSITE_REMOTE" --min-age "${OFFSITE_KEEP_DAYS}d" \
        || log "WARNING: could not remove old off-site copies"
fi

date '+%Y-%m-%dT%H:%M:%S%z' > "$BACKUP_DIR/last_success"
log "backup complete"
