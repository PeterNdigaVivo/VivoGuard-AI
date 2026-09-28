"""scripts/backup_db.sh — run end to end against a fake `docker`, plus
the retention (prune) logic. No real database or Docker is needed."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup_db.sh"
RESTORE_TEST = ROOT / "scripts" / "restore_test.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="needs bash and flock (util-linux)")

# Stands in for `docker inspect` / `docker exec ... sh -c "<cmd>"`.
FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$1" in
  inspect) echo "${FAKE_PG_RUNNING:-true}" ;;
  exec)
    shift; [ "$1" = "-i" ] && shift; shift     # drop -i and container name
    cmd="$3"                                    # sh -c "<cmd>"
    case "$cmd" in
      *psql*)       printf 'users 3\nstores 2\ncameras 5\nalerts 10\ndetection_events 40\n' ;;
      *pg_dump*)    [ -n "${FAKE_DUMP_FAIL:-}" ] && exit 1; printf 'PGDMP-fake-dump' ;;
      *pg_restore*) cat > /dev/null; [ -n "${FAKE_LIST_FAIL:-}" ] && exit 1; exit 0 ;;
    esac ;;
esac
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    backups = tmp_path / "backups"
    e = dict(os.environ,
             PATH=f"{bindir}:{os.environ['PATH']}",
             VG_BACKUP_CONFIG=str(tmp_path / "no-such-config"),
             BACKUP_DIR=str(backups),
             MIN_FREE_MB="1")
    return e, backups


def _run(env, *args):
    return subprocess.run(["bash", str(BACKUP), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def _sets(d: Path) -> list[str]:
    return sorted(p.name for p in d.glob("vivoguard-*.dump"))


def test_scripts_have_valid_syntax():
    for script in (BACKUP, RESTORE_TEST):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_full_backup_writes_dump_counts_checksum(env):
    e, backups = env
    r = _run(e)
    assert r.returncode == 0, r.stderr
    daily = backups / "daily"
    dumps = list(daily.glob("vivoguard-*.dump"))
    assert len(dumps) == 1
    stem = dumps[0].with_suffix("")
    assert dumps[0].read_text() == "PGDMP-fake-dump"
    assert "cameras 5" in Path(f"{stem}.counts").read_text()
    assert Path(f"{stem}.sha256").exists()
    assert (backups / "last_success").exists()
    assert not list(daily.glob("*.partial"))
    # Backups are root-only.
    assert stat.S_IMODE(dumps[0].stat().st_mode) == 0o600


def test_failed_dump_keeps_nothing_and_reports(env):
    e, backups = env
    r = _run(dict(e, FAKE_DUMP_FAIL="1"))
    assert r.returncode != 0
    assert "BACKUP FAILED" in r.stderr
    assert list((backups / "daily").iterdir()) == []   # no half-made set
    assert not (backups / "last_success").exists()


def test_unreadable_dump_is_not_kept(env):
    e, backups = env
    r = _run(dict(e, FAKE_LIST_FAIL="1"))
    assert r.returncode != 0
    assert "not readable" in r.stderr
    assert list((backups / "daily").iterdir()) == []


def test_refuses_when_postgres_not_running(env):
    e, _ = env
    r = _run(dict(e, FAKE_PG_RUNNING="false"))
    assert r.returncode != 0
    assert "is not running" in r.stderr


def test_refuses_when_disk_nearly_full(env):
    e, _ = env
    r = _run(dict(e, MIN_FREE_MB="999999999"))
    assert r.returncode != 0
    assert "free" in r.stderr


def _make_sets(d: Path, n: int) -> list[str]:
    d.mkdir(parents=True, exist_ok=True)
    stamps = [f"2026-09-{day:02d}T031500" for day in range(1, n + 1)]
    for s in stamps:
        for suffix in (".dump", ".counts", ".sha256", "-data.tar.gz"):
            (d / f"vivoguard-{s}{suffix}").write_text("x")
    return stamps


def test_prune_keeps_newest_sets_with_their_companion_files(env, tmp_path):
    e, _ = env
    d = tmp_path / "prune"
    stamps = _make_sets(d, 10)
    (d / "unrelated.txt").write_text("keep me")
    r = _run(e, "--prune", str(d), "7")
    assert r.returncode == 0, r.stderr
    assert _sets(d) == [f"vivoguard-{s}.dump" for s in stamps[3:]]
    for s in stamps[:3]:
        assert not list(d.glob(f"vivoguard-{s}*"))
    assert len(list(d.glob(f"vivoguard-{stamps[-1]}*"))) == 4
    assert (d / "unrelated.txt").exists()


def test_prune_with_fewer_sets_than_limit_deletes_nothing(env, tmp_path):
    e, _ = env
    d = tmp_path / "prune"
    _make_sets(d, 3)
    assert _run(e, "--prune", str(d), "7").returncode == 0
    assert len(_sets(d)) == 3


@pytest.mark.parametrize("keep", ["0", "abc"])
def test_prune_rejects_bad_keep_count(env, tmp_path, keep):
    e, _ = env
    d = tmp_path / "prune"
    _make_sets(d, 3)
    r = _run(e, "--prune", str(d), keep)
    assert r.returncode != 0
    assert len(_sets(d)) == 3


def test_nightly_run_applies_retention(env):
    e, backups = env
    _make_sets(backups / "daily", 8)
    r = _run(dict(e, KEEP_DAILY="7"))
    assert r.returncode == 0, r.stderr
    kept = _sets(backups / "daily")
    assert len(kept) == 7
    assert kept[0] == "vivoguard-2026-09-03T031500.dump"   # two oldest removed


# ── restore_test.sh against a fake `docker` ───────────────────────────

FAKE_DOCKER_RESTORE = r"""#!/usr/bin/env bash
case "$1" in
  inspect) echo "postgres:16-alpine" ;;
  run)     echo "fake-container-id" ;;
  rm)      exit 0 ;;
  exec)
    shift; [ "$1" = "-i" ] && shift; shift     # drop -i and container name
    case "$*" in
      pg_isready*)            exit 0 ;;
      pg_restore*)            cat > /dev/null; [ -n "${FAKE_RESTORE_FAIL:-}" ] && exit 1; exit 0 ;;
      *alembic_version*)      echo "0099_fake" ;;
      *'count(*)'*)           echo "${FAKE_RESTORED_COUNT:-5}" ;;
    esac ;;
esac
"""


@pytest.fixture
def restore_env(env):
    e, backups = env
    docker = Path(e["PATH"].split(":")[0]) / "docker"
    docker.write_text(FAKE_DOCKER_RESTORE)
    daily = backups / "daily"
    daily.mkdir(parents=True)
    dump = daily / "vivoguard-2026-09-28T031500.dump"
    dump.write_text("PGDMP-fake-dump")
    (daily / "vivoguard-2026-09-28T031500.counts").write_text("users 3\ncameras 5\n")
    subprocess.run(["sha256sum", dump.name], cwd=daily, check=True,
                   stdout=(daily / "vivoguard-2026-09-28T031500.sha256").open("w"))
    return e, dump


def _restore(env, *args):
    return subprocess.run(["bash", str(RESTORE_TEST), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_restore_test_passes_when_counts_match(restore_env):
    e, _ = restore_env
    r = _restore(e)            # newest backup is picked automatically
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout
    assert "0099_fake" in r.stdout


def test_restore_test_fails_when_rows_are_missing(restore_env):
    e, _ = restore_env
    r = _restore(dict(e, FAKE_RESTORED_COUNT="4"))   # cameras: 4 < 5
    assert r.returncode != 0
    assert "FAIL" in r.stderr


def test_restore_test_fails_when_pg_restore_errors(restore_env):
    e, dump = restore_env
    r = _restore(dict(e, FAKE_RESTORE_FAIL="1"), str(dump))
    assert r.returncode != 0
    assert "pg_restore reported errors" in r.stderr


def test_restore_test_detects_damaged_file(restore_env):
    e, dump = restore_env
    dump.write_text("PGDMP-corrupted")
    r = _restore(e, str(dump))
    assert r.returncode != 0
    assert "checksum mismatch" in r.stderr
