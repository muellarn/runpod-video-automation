import httpx
import pytest

from runpod_video_automation.runpod_client import RunPodClient


def test_find_or_create_volume_reuses_matching_volume() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "volume-1",
                    "name": "models",
                    "size": 250,
                    "dataCenterId": "EU-RO-1",
                }
            ],
        )

    client = RunPodClient("test-key", transport=httpx.MockTransport(handler))
    try:
        volume, created = client.find_or_create_volume(
            name="models", size=250, data_center_id="EU-RO-1"
        )
    finally:
        client.close()

    assert volume["id"] == "volume-1"
    assert created is False


def test_find_volume_rejects_undersized_existing_volume() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "volume-1",
                    "name": "models",
                    "size": 50,
                    "dataCenterId": "EU-RO-1",
                }
            ],
        )

    with RunPodClient("test-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Exception, match="below required"):
            client.find_volume(
                name="models", minimum_size=100, data_center_id="EU-RO-1"
            )


def test_create_pod_does_not_expose_comfyui_port() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(201, json={"id": "pod-1"})

    client = RunPodClient("test-key", transport=httpx.MockTransport(handler))
    try:
        pod = client.create_pod(
            name="video",
            image="image:tag",
            gpu_type_ids=("GPU",),
            network_volume_id="volume-1",
            public_key="ssh-ed25519 test",
            container_disk_gb=50,
            min_ram_per_gpu=64,
            min_vcpu_per_gpu=8,
        )
    finally:
        client.close()

    assert pod["id"] == "pod-1"
    assert captured["ports"] == ["22/tcp"]
    assert captured["gpuTypePriority"] == "custom"
    assert captured["volumeMountPath"] == "/runpod-volume"
    assert "dataCenterIds" not in captured
    assert captured["env"]["SERVE_API_LOCALLY"] == "true"
    assert "RUNPOD_S3_ACCESS_KEY_ID" not in captured["env"]
    assert "RUNPOD_S3_SECRET_ACCESS_KEY" not in captured["env"]


def test_terminate_pod_retries_transient_server_error() -> None:
    statuses = iter([500, 204])
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = next(statuses)
        payload = {"error": "temporary"} if status == 500 else None
        return httpx.Response(status, json=payload)

    client = RunPodClient(
        "test-key",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    try:
        client.terminate_pod("pod-1")
    finally:
        client.close()

    assert [request.method for request in requests] == ["DELETE", "DELETE"]
    assert sleeps == [1]


def test_stop_and_start_pod_use_lifecycle_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = RunPodClient("test-key", transport=httpx.MockTransport(handler))
    try:
        client.stop_pod("pod-1")
        client.start_pod("pod-1")
    finally:
        client.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1/pods/pod-1/stop"),
        ("POST", "/v1/pods/pod-1/start"),
    ]
