import json
from collections.abc import Callable

import httpx

from runpod_video_automation.comfy_client import ComfyClient


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = iter(json.dumps(message) for message in messages)
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def recv(self) -> str:
        return next(self.messages)

    def close(self) -> None:
        self.closed = True


def test_queue_and_wait_reports_websocket_progress() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if request.url.path == "/history/prompt-1":
            return httpx.Response(
                200,
                json={"prompt-1": {"status": {"status_str": "success"}}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    socket = FakeWebSocket(
        [
            {
                "type": "executing",
                "data": {"prompt_id": "prompt-1", "node": "57"},
            },
            {
                "type": "progress",
                "data": {
                    "prompt_id": "prompt-1",
                    "node": "57",
                    "value": 5,
                    "max": 20,
                },
            },
            {
                "type": "execution_success",
                "data": {"prompt_id": "prompt-1"},
            },
        ]
    )
    websocket_urls: list[str] = []

    def websocket_factory(url: str, **_: object) -> FakeWebSocket:
        websocket_urls.append(url)
        return socket

    statuses: list[str] = []
    client = ComfyClient(
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
        websocket_factory=websocket_factory,
    )
    try:
        prompt_id, history = client.queue_and_wait(
            {"57": {"class_type": "KSamplerAdvanced", "inputs": {}}},
            timeout_seconds=60,
            status_callback=statuses.append,
        )
    finally:
        client.close()

    assert prompt_id == "prompt-1"
    assert history["status"]["status_str"] == "success"
    assert websocket_urls[0].startswith("ws://127.0.0.1:8188/ws?clientId=")
    queued = json.loads(requests[0].content)
    assert queued["client_id"] == websocket_urls[0].split("clientId=", 1)[1]
    assert "ComfyUI node 57: KSamplerAdvanced" in statuses
    assert "ComfyUI progress 57 KSamplerAdvanced: 5/20 (25%)" in statuses
    assert statuses[-1] == "ComfyUI execution completed"
    assert socket.closed is True


def test_interrupt_and_clear_stops_execution_and_pending_queue() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client = ComfyClient(
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
    )
    try:
        client.interrupt_and_clear()
    finally:
        client.close()

    assert [request.url.path for request in requests] == ["/interrupt", "/queue"]
    assert json.loads(requests[1].content) == {"clear": True}


def test_queue_is_idle_checks_running_and_pending_jobs() -> None:
    responses = iter(
        [
            {"queue_running": [[1]], "queue_pending": []},
            {"queue_running": [], "queue_pending": []},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/queue"
        return httpx.Response(200, json=next(responses))

    client = ComfyClient(
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.queue_is_idle() is False
        assert client.queue_is_idle() is True
    finally:
        client.close()
