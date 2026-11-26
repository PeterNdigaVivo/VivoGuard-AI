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
DOCKER_STUB = r"""
docker() {
  case "$1:$2" in
    compose:ps)
      if [ "$6" = "recorder" ]; then
        [ "$VERIFY_SCENARIO" != "missing" ] || return 0
        printf '%s\n' recorder-1 recorder-2
      else
        printf '%s\n' "$6-1"
      fi
      ;;
    exec:*)
      if [ "$2" = recorder-2 ] && [ "$VERIFY_SCENARIO" = "mismatch" ]; then
        printf '%s\n' stale-image
      elif [ "$2" = recorder-2 ] && [ "$VERIFY_SCENARIO" = "unreadable" ]; then
        return 1
      else
        git show "HEAD:backend/${7#/app/}" | sha256sum
      fi
      ;;
    *) return 1 ;;
  esac
}
export -f docker
bash scripts/verify-release-sources.sh
"""


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("match", "Verified API, inference and recorder source checks"),
        ("missing", "no running container for recorder"),
        ("mismatch", "recorder source mismatch"),
        ("unreadable", "recorder source mismatch"),
    ],
)
def test_verify_running_release_sources(scenario, message):
    if BASH is None or not Path(BASH).is_file():
        pytest.skip("Bash is required by the Compose deployment scripts")

    result = subprocess.run(
        [str(BASH), "-c", DOCKER_STUB],
        cwd=ROOT,
        env={**os.environ, "VERIFY_SCENARIO": scenario},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert (result.returncode == 0) == (scenario == "match"), result.stderr
    assert message in result.stdout + result.stderr


def test_deploy_verifies_running_sources_after_container_replacement():
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert (
        script.index("docker compose restart nginx")
        < script.index('bash scripts/verify-release-sources.sh "$release_revision"')
        < script.index("Deploy complete.")
    )
