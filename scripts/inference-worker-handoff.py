#!/usr/bin/env python3
"""Stop one Compose inference worker and release its confirmed long claims.

Run under the deployment lock with the old image pinned for rollback. The
caller then starts the replacement. Camera and task identities stay in memory.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


class HandoffError(RuntimeError):
    """A condition that prevents a safe handoff."""


class Container(NamedTuple):
    id: str
    status: str
    started_at: str
    service: str
    project: str


_RELEASE_PAIR = """
if redis.call('get', KEYS[1]) ~= ARGV[1] or redis.call('get', KEYS[2]) ~= ARGV[1] then
    return 0
end
return redis.call('del', KEYS[1], KEYS[2])
"""


def docker(*args: str, input_text: str | None = None, timeout: int = 30) -> str:
    output = subprocess.run(
        ["docker", *args], input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if output.returncode:
        raise HandoffError(f"Docker {args[0]} failed during inference handoff")
    return output.stdout.strip()


def container(ref: str) -> Container:
    template = (
        '{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|'
        '{{index .Config.Labels "com.docker.compose.service"}}|'
        '{{index .Config.Labels "com.docker.compose.project"}}'
    )
    return Container(*docker("inspect", "--type", "container", "--format", template, ref).split("|"))


def long_claims(redis, inference) -> dict[str, str]:
    claims = {}
    keys = list(redis.scan_iter(match=f"{inference.LOCK_KEY_PREFIX}*", count=200))
    if not keys:
        return claims
    pipe = redis.pipeline(transaction=True)
    for lock in keys:
        camera = int(lock.rsplit(":", 1)[1])
        pipe.get(lock)
        pipe.get(inference.HB_KEY_FMT.format(camera_id=camera))
        pipe.ttl(lock)
    rows = pipe.execute()
    for lock, owner, beat, ttl in zip(keys, rows[::3], rows[1::3], rows[2::3]):
        if ttl > inference.LOCK_TTL and beat:
            if owner != beat:
                raise HandoffError("A long inference claim has an inconsistent owner")
            claims[lock] = owner
    return claims


def capture(redis, inference, control) -> dict[str, str]:
    node = "inference-worker@" + socket.gethostname()
    routes = control.inspect(timeout=5).active_queues() or {}
    required_queues = set(inference._inference_queue_names()) | {"default"}
    consumers = {
        worker for worker, queues in routes.items()
        if required_queues.intersection(queue["name"] for queue in queues)
    }
    if consumers != {node} or not required_queues.issubset(
        queue["name"] for queue in routes.get(node, [])
    ):
        raise HandoffError("A single local inference consumer could not be confirmed")
    active = (control.inspect(destination=[node], timeout=5).active() or {}).get(node)
    if active is None:
        raise HandoffError("The inference worker did not confirm its active tasks")
    owned = {}
    for task in active:
        args = task.get("args")
        if (task.get("name") != "inference.run_camera" or not isinstance(args, (list, tuple))
                or len(args) != 1 or type(args[0]) is not int or not task.get("id")):
            raise HandoffError("The inference worker has other or unrecognized active work")
        owned[inference.LOCK_KEY_FMT.format(camera_id=args[0])] = task["id"]
    claims = long_claims(redis, inference)
    if any(owned.get(key) != owner for key, owner in claims.items()):
        raise HandoffError("A long inference claim was not proven to belong to this worker")
    return claims


def redis_identity(redis) -> tuple[str, int]:
    return redis.info("server")["run_id"], int(redis.connection_pool.connection_kwargs.get("db", 0))


def release(redis, inference, claims: dict[str, str]) -> int:
    remaining = long_claims(redis, inference)
    if any(claims.get(key) != owner for key, owner in remaining.items()):
        raise HandoffError("The long inference claims changed during shutdown")
    count = 0
    for lock, owner in remaining.items():
        camera = int(lock.rsplit(":", 1)[1])
        heartbeat = inference.HB_KEY_FMT.format(camera_id=camera)
        count += int(redis.eval(_RELEASE_PAIR, 2, lock, heartbeat, owner) == 2)
    return count


def runtime(mode: str) -> None:
    import redis
    from app.config import settings
    from app.tasks import inference
    from app.tasks.celery_app import celery_app

    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    with client:
        identity = list(redis_identity(client))
        if mode == "capture":
            print(json.dumps({"redis": identity, "claims": capture(client, inference, celery_app.control)}))
        elif mode == "identify":
            print(json.dumps(identity))
        else:
            snapshot = json.load(sys.stdin)
            if snapshot["redis"] != identity:
                raise HandoffError("The Redis server or database changed during handoff")
            print(release(client, inference, snapshot["claims"]))


def handoff(worker: str, peer: str) -> int:
    old, helper = container(worker), container(peer)
    if (old.service != "worker-inference" or helper.service != "worker-alerts"
            or not old.project or old.project != helper.project
            or old.status != "running" or helper.status != "running"):
        raise HandoffError("Expected running inference and alerts services in the same Compose project")
    source = Path(__file__).read_text(encoding="utf-8")
    snapshot = docker("exec", old.id, "python", "-c", source, "capture")
    identity = docker("exec", helper.id, "python", "-c", source, "identify")
    if json.loads(snapshot)["redis"] != json.loads(identity):
        raise HandoffError("The inference and alerts workers use different Redis databases")
    if container(old.id) != old:
        raise HandoffError("The inference worker changed before shutdown")
    stopped = old._replace(status="exited")
    try:
        docker("stop", "--time", "45", old.id, timeout=70)
        if container(old.id) != stopped or container(helper.id) != helper:
            raise HandoffError("The selected worker did not stop or the alerts worker changed")
        return int(docker(
            "exec", "-i", helper.id, "python", "-c", source, "release", input_text=snapshot,
        ))
    except Exception:
        if container(old.id) == stopped:
            docker("start", old.id)
        raise


def main() -> None:
    if sys.argv[1:] in (["capture"], ["identify"], ["release"]):
        runtime(sys.argv[1])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker")
    parser.add_argument("alerts_peer")
    options = parser.parse_args()
    count = handoff(options.worker, options.alerts_peer)
    print(f"Stopped the previous inference worker and released {count} verified long claims")


if __name__ == "__main__":
    try:
        main()
    except HandoffError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"Inference handoff failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
