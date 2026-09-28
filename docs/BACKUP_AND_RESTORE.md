# Backups and restoring VivoGuard

Every night at **03:15 Nairobi time** the server saves a full copy of the
VivoGuard database: users, stores, cameras, zones, alert history,
training labels and settings. It keeps the last **7 nightly** copies and
the last **4 Sunday** copies in `/var/backups/vivoguard/`, readable only
by root. Off-site copies are optional (see step 5 of the setup).

---

## ⚠️ Before anything else: keep a copy of `.env` somewhere safe

The backups deliberately do **not** contain the server's `.env` file,
because it holds passwords and keys. But the database backup cannot be
fully used without one value in it:

- **`CREDENTIALS_FERNET_KEY`** unlocks the camera and recorder (NVR)
  passwords stored in the database. Without the *same* key, a restored
  system cannot log in to any camera.

So: copy the whole `.env` file from `/root/VivoGuard-AI/.env` into your
company password manager now, and again whenever it changes. Never put
it in GitHub, email or chat.

---

## One-time setup

Run these on the server as root:

```bash
cd /root/VivoGuard-AI && git pull

# 1. Folders for the backups and their settings (root-only).
install -d -m 700 /var/backups/vivoguard /etc/vivoguard

# 2. Settings file. The defaults are fine; edit only to change them.
cp deploy/backup/backup.env.example /etc/vivoguard/backup.env
chmod 600 /etc/vivoguard/backup.env

# 3. Install the nightly schedule.
cp deploy/backup/vivoguard-backup.service deploy/backup/vivoguard-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vivoguard-backup.timer

# 4. Take the first backup now, then prove it restores.
systemctl start vivoguard-backup.service
scripts/restore_test.sh          # must end with: PASS
```

**5. Optional: copies off the server.** Install `rclone`, then run
`rclone config` and create an encrypted ("crypt") remote, for example
`vivoguard-crypt`, on top of Google Drive, S3 or Backblaze. rclone keeps
that login in its own file on the server, not in VivoGuard. Then set
`OFFSITE_REMOTE=vivoguard-crypt:backups` in `/etc/vivoguard/backup.env`.
Off-site copies older than 35 days are deleted automatically.

**6. Optional: model and training files.** `data/models` and
`data/training` are folders, not database rows. To include them, check
their size with `du -sh /root/VivoGuard-AI/data/*`, then set
`INCLUDE_DATA_DIRS` in `/etc/vivoguard/backup.env`. Every nightly copy
stores them again, so make sure the disk has room.

---

## Checking that backups are working

```bash
cat /var/backups/vivoguard/last_success            # time of the last good backup
ls -lh /var/backups/vivoguard/daily/               # the nightly copies
systemctl list-timers vivoguard-backup.timer       # when the next one runs
journalctl -u vivoguard-backup.service -n 50       # what the last run said
```

If a run fails, `systemctl status vivoguard-backup.service` shows
**failed** and the log line starting `BACKUP FAILED:` says why (for
example not enough disk space, or the database container not running).

**Once a month**, run `scripts/restore_test.sh` and check it says
**PASS**. It loads the newest backup into a temporary, network-isolated
database, compares the numbers of users, stores, cameras, alerts and
detection events with those recorded at backup time, then deletes the
temporary database. It never touches the live system.

---

## Restoring onto this server

Use this when the database is damaged or data was deleted by mistake.
**Everything recorded after the chosen backup is lost** (for example
alerts from the night the backup was taken), so pick the newest backup
from before the problem.

```bash
cd /root/VivoGuard-AI

# 1. Choose the backup and test it first.
ls -lh /var/backups/vivoguard/daily/ /var/backups/vivoguard/weekly/
BACKUP=/var/backups/vivoguard/daily/vivoguard-YYYY-MM-DDTHHMMSS.dump   # ← fill in
scripts/restore_test.sh "$BACKUP"            # must say PASS

# 2. If the current database still works, save it first, just in case.
systemctl start vivoguard-backup.service

# 3. Stop everything, then start only the database.
docker compose stop
docker compose up -d postgres

# 4. Replace the database with the backup.
docker exec -i vivoguard-postgres sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker exec -i vivoguard-postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < "$BACKUP"

# 5. Bring the database up to the current code version, then start all.
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d
```

If you normally start Docker with extra `-f …` files, add the same ones
to every `docker compose` command above.

Then log in to the website and check that stores, cameras and recent
alerts are there.

---

## Restoring onto a new server (the old one is gone)

1. Install Docker on the new server and clone the repository to
   `/root/VivoGuard-AI`, on the same branch the old server ran.
2. Put the `.env` file back from the password manager. It **must** have
   the same `CREDENTIALS_FERNET_KEY`. Other values can stay the same.
3. Get the newest backup file onto the server, e.g. from off-site
   storage: `rclone copy vivoguard-crypt:backups/daily /var/backups/vivoguard/daily`
   (after setting up the same rclone remote), or copy it over with `scp`.
4. Start only the database: `docker compose up -d postgres`
5. Follow **steps 1, 4 and 5** of "Restoring onto this server" above.
6. Do the one-time backup setup on the new server too.

---

## What is (and is not) in a backup

| Included | Not included |
|---|---|
| The whole database: users, stores, cameras, zones, alert and detection history, training labels, settings | `.env` (keep it in the password manager) |
| Row counts and a checksum to verify each copy | Video recordings and snapshots in `data/recordings`, `data/thumbnails` |
| Optionally: `data/models`, `data/training` | Anything sent to GitHub: backups live only on the server and off-site storage |
