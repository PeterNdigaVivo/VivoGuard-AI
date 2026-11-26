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


ROLLBACK_STUB = r"""
first=$(printf 'a%.0s' {1..64})
second=$(printf 'b%.0s' {1..64})
docker() {
  case "$1:$2" in
    compose:ps)
      case "$6" in
        recorder) printf '%s\n' recorder-1 recorder-2 ;;
        frontend) return 0 ;;
        *) return 1 ;;
      esac
      ;;
    container:inspect)
      case "$5" in
        recorder-1) printf 'sha256:%s\n' "$first" ;;
        recorder-2) printf 'sha256:%s\n' "$second" ;;
        *) return 1 ;;
      esac
      ;;
    image:inspect)
      case "$5" in
        sha256:"$first"|vivoguard/rollback:recorder-"$first")
          printf 'sha256:%s\n' "$first"
          ;;
        sha256:"$second")
          [ "$ROLLBACK_SCENARIO" != "missing_image" ] || return 1
          printf 'sha256:%s\n' "$second"
          ;;
        vivoguard/rollback:recorder-"$second")
          if [ "$ROLLBACK_SCENARIO" = "tag_mismatch" ]; then
            printf 'sha256:%s\n' "$first"
          else
            printf 'sha256:%s\n' "$second"
          fi
          ;;
        *) return 1 ;;
      esac
      ;;
    image:tag)
      printf 'tag %s %s\n' "$3" "$4"
      ;;
    *) return 1 ;;
  esac
}
export first second
export -f docker
bash scripts/pin-rollback-images.sh recorder frontend
"""


@pytest.mark.parametrize(
    ("scenario", "success", "tagged_second"),
    [("match", True, True), ("missing_image", False, False), ("tag_mismatch", False, True)],
)
def test_pin_rollback_uses_each_running_image_and_verifies_the_tag(
    scenario, success, tagged_second,
):
    if BASH is None or not Path(BASH).is_file():
        pytest.skip("Bash is required by the Compose deployment scripts")

    result = subprocess.run(
        [str(BASH), "-c", ROLLBACK_STUB],
        cwd=ROOT,
        env={**os.environ, "ROLLBACK_SCENARIO": scenario},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert (result.returncode == 0) is success
    first, second = "a" * 64, "b" * 64
    assert f"tag sha256:{first} vivoguard/rollback:recorder-{first}" in result.stdout
    assert (f"tag sha256:{second} vivoguard/rollback:recorder-{second}" in result.stdout) is tagged_second
    assert "frontend" not in result.stdout
    if not success:
        assert "Rollback preflight failed" in result.stderr


def test_deploy_pins_rollback_images_before_building_or_migrating():
    script = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert (
        script.index('bash scripts/pin-rollback-images.sh "${app_services[@]}"')
        < script.index("docker compose build api")
        < script.index("alembic upgrade head")
    )
