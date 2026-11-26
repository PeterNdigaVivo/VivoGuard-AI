import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BASH = (
    Path(shutil.which("git")).resolve().parents[1] / "bin" / "bash.exe"
    if os.name == "nt" and shutil.which("git")
    else shutil.which("bash")
)
STUB = r"""
git() {
  case "$1" in
    rev-parse) printf 'cccccccccccccccccccccccccccccccccccccccc\n' ;;
    merge-base)
      [ "$2" = --is-ancestor ] || return 2
      printf 'checked %s\n' "$3"
      [ "$3" = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]
      ;;
    *) return 2 ;;
  esac
}
docker() {
  case "$1:$2" in
    compose:ps)
      case "$6" in
        api) printf 'api-1\napi-2\n' ;;
        worker-alerts) [ "$SCENARIO" != list_error ] || return 1 ;;
        *) return 2 ;;
      esac
      ;;
    container:inspect)
      case "$5:$SCENARIO" in
        api-1:*) printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
        api-2:included|api-2:list_error) printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' ;;
        api-2:legacy) printf '\n' ;;
        api-2:missing_release) printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n' ;;
        api-2:bad_label) printf 'invalid-label\n' ;;
        api-2:inspect_error) return 1 ;;
        *) return 2 ;;
      esac
      ;;
    *) return 2 ;;
  esac
}
export -f docker git
bash scripts/verify-release-ancestry.sh HEAD api worker-alerts
"""


@pytest.mark.parametrize(
    ("scenario", "success", "diagnostic"),
    [
        ("included", True, ""),
        ("legacy", True, ""),
        ("missing_release", False, "api revision " + "b" * 40 + " is absent"),
        ("bad_label", False, "invalid running revision label for api"),
        ("inspect_error", False, "cannot inspect the running revision for api"),
        ("list_error", False, "cannot list running containers for worker-alerts"),
    ],
)
def test_release_preflight_checks_live_revisions(scenario, success, diagnostic):
    if BASH is None or not Path(BASH).is_file():
        pytest.skip("Bash is required by the Compose deployment scripts")

    result = subprocess.run(
        [str(BASH), "-c", STUB],
        cwd=ROOT,
        env={**os.environ, "SCENARIO": scenario},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert (result.returncode == 0) is success, result.stderr
    assert "checked " + "a" * 40 in result.stdout
    if diagnostic:
        assert diagnostic in result.stderr


def test_deploy_checks_release_ancestry_before_building_or_migrating():
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert (
        script.index('bash scripts/verify-release-ancestry.sh "$release_revision" api worker-alerts')
        < script.index('bash scripts/pin-rollback-images.sh')
        < script.index("docker compose build api")
        < script.index("alembic upgrade head")
    )
