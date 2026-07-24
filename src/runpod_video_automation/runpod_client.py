from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx


class RunPodError(RuntimeError):
    """A RunPod control-plane operation failed."""


class RunPodClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url="https://rest.runpod.io/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RunPodClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.is_error:
            detail = response.text[:1000]
            raise RunPodError(f"RunPod {method} {path} failed ({response.status_code}): {detail}")
        if not response.content:
            return None
        return response.json()

    def list_pods(self) -> list[dict[str, Any]]:
        value = self._request("GET", "/pods")
        return value if isinstance(value, list) else []

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        value = self._request("GET", f"/pods/{pod_id}")
        if not isinstance(value, dict):
            raise RunPodError(f"RunPod returned an invalid pod response for {pod_id}")
        return value

    def create_pod(
        self,
        *,
        name: str,
        image: str,
        gpu_type_ids: tuple[str, ...],
        network_volume_id: str,
        public_key: str,
        container_disk_gb: int,
        min_ram_per_gpu: int,
        min_vcpu_per_gpu: int,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "imageName": image,
            "gpuTypeIds": list(gpu_type_ids),
            "gpuTypePriority": "custom",
            "gpuCount": 1,
            "cloudType": "SECURE",
            "networkVolumeId": network_volume_id,
            "volumeMountPath": "/runpod-volume",
            "containerDiskInGb": container_disk_gb,
            "ports": ["22/tcp"],
            "minRAMPerGPU": min_ram_per_gpu,
            "minVCPUPerGPU": min_vcpu_per_gpu,
            "interruptible": False,
            "env": {
                "PUBLIC_KEY": public_key.strip(),
                "SERVE_API_LOCALLY": "true",
                "COMFY_LOG_LEVEL": "INFO",
            },
        }
        value = self._request("POST", "/pods", json=payload)
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise RunPodError(f"RunPod returned an invalid create response: {value!r}")
        return value

    def terminate_pod(self, pod_id: str) -> None:
        path = f"/pods/{pod_id}"
        for attempt in range(5):
            response = self._client.delete(path)
            if not response.is_error or response.status_code == 404:
                return
            if response.status_code < 500 or attempt == 4:
                detail = response.text[:1000]
                raise RunPodError(
                    f"RunPod DELETE {path} failed ({response.status_code}): {detail}"
                )
            self._sleep(2**attempt)

    def stop_pod(self, pod_id: str) -> None:
        self._request("POST", f"/pods/{pod_id}/stop")

    def start_pod(self, pod_id: str) -> None:
        self._request("POST", f"/pods/{pod_id}/start")

    def list_network_volumes(self) -> list[dict[str, Any]]:
        value = self._request("GET", "/networkvolumes")
        return value if isinstance(value, list) else []

    def create_network_volume(
        self, *, name: str, size: int, data_center_id: str
    ) -> dict[str, Any]:
        value = self._request(
            "POST",
            "/networkvolumes",
            json={"name": name, "size": size, "dataCenterId": data_center_id},
        )
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise RunPodError(f"RunPod returned an invalid volume response: {value!r}")
        return value

    def find_or_create_volume(
        self, *, name: str, size: int, data_center_id: str
    ) -> tuple[dict[str, Any], bool]:
        matches = [
            volume
            for volume in self.list_network_volumes()
            if volume.get("name") == name
        ]
        if len(matches) > 1:
            raise RunPodError(f"Multiple network volumes are named {name!r}")
        if matches:
            volume = matches[0]
            if volume.get("dataCenterId") != data_center_id:
                raise RunPodError(
                    f"Volume {name!r} is in {volume.get('dataCenterId')}, not {data_center_id}"
                )
            return volume, False
        return self.create_network_volume(
            name=name, size=size, data_center_id=data_center_id
        ), True

    def wait_until_running(
        self, pod_id: str, *, timeout_seconds: int = 900
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            pod = self.get_pod(pod_id)
            status = pod.get("desiredStatus") or pod.get("status")
            mappings = pod.get("portMappings") or {}
            if (
                status == "RUNNING"
                and pod.get("publicIp")
                and (mappings.get("22") or mappings.get(22))
            ):
                return pod
            if status in {"EXITED", "ERROR", "TERMINATED"}:
                raise RunPodError(f"Pod {pod_id} entered terminal state {status}")
            self._sleep(10)
        raise TimeoutError(f"Pod {pod_id} was not ready after {timeout_seconds} seconds")
