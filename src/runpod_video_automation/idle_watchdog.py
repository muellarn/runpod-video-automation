from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from pathlib import Path

from runpod_video_automation.comfy_client import ComfyClient
from runpod_video_automation.remote import RemoteWorker
from runpod_video_automation.runpod_client import RunPodClient


def wait_for_idle_and_stop(
    runpod: RunPodClient,
    comfy: ComfyClient,
    pod_id: str,
    *,
    idle_seconds: float,
    poll_interval: float = 15,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    idle_since: float | None = None
    while True:
        now = monotonic()
        if comfy.queue_is_idle():
            idle_since = now if idle_since is None else idle_since
            if now - idle_since >= idle_seconds:
                if comfy.queue_is_idle():
                    runpod.stop_pod(pod_id)
                    return
                idle_since = None
        else:
            idle_since = None
        sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pod_id")
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--idle-minutes", type=float, required=True)
    parser.add_argument("--start-timeout", type=int, default=900)
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY is not set")
    with RunPodClient(api_key) as runpod:
        pod = runpod.wait_until_running(args.pod_id, timeout_seconds=args.start_timeout)
        mappings = pod.get("portMappings") or {}
        ssh_port = int(mappings.get("22") or mappings[22])
        remote = RemoteWorker(
            host=str(pod["publicIp"]),
            port=ssh_port,
            ssh_key=args.ssh_key.expanduser(),
        )
        remote.wait_for_ssh()
        with remote.comfy_tunnel() as base_url:
            comfy = ComfyClient(base_url)
            try:
                comfy.wait_until_ready()
                wait_for_idle_and_stop(
                    runpod,
                    comfy,
                    args.pod_id,
                    idle_seconds=args.idle_minutes * 60,
                )
            finally:
                comfy.close()


if __name__ == "__main__":
    main()
