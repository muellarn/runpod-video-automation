from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import websocket

from runpod_video_automation.workflow import collect_output_files


class ComfyClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        websocket_factory: Callable[..., websocket.WebSocket] = websocket.create_connection,
    ) -> None:
        self._base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            timeout=120,
            transport=transport,
        )
        self._websocket_factory = websocket_factory
        self.runtime_metadata: dict[str, Any] = {}

    def close(self) -> None:
        self._client.close()

    def interrupt_and_clear(self) -> None:
        response = self._client.post("/interrupt")
        response.raise_for_status()
        response = self._client.post("/queue", json={"clear": True})
        response.raise_for_status()

    def queue_is_idle(self) -> bool:
        response = self._client.get("/queue")
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(f"ComfyUI returned an invalid queue response: {value!r}")
        running = value.get("queue_running")
        pending = value.get("queue_pending")
        if not isinstance(running, list) or not isinstance(pending, list):
            raise RuntimeError(f"ComfyUI returned an invalid queue response: {value!r}")
        return not running and not pending

    def wait_until_ready(self, timeout_seconds: int = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = self._client.get("/system_stats")
                response.raise_for_status()
                value = response.json()
                return value if isinstance(value, dict) else {}
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                time.sleep(3)
        raise TimeoutError(f"ComfyUI did not become ready: {last_error}")

    def upload_image(self, path: Path, remote_name: str | None = None) -> str:
        name = remote_name or path.name
        with path.open("rb") as image_file:
            response = self._client.post(
                "/upload/image",
                files={"image": (name, image_file), "overwrite": (None, "true")},
            )
        response.raise_for_status()
        value = response.json()
        return str(value.get("name", name)) if isinstance(value, dict) else name

    def queue(
        self, workflow: dict[str, Any], *, client_id: str | None = None
    ) -> str:
        response = self._client.post(
            "/prompt",
            json={"prompt": workflow, "client_id": client_id or str(uuid.uuid4())},
        )
        if response.status_code == 400:
            raise RuntimeError(f"ComfyUI rejected the workflow: {response.text[:4000]}")
        response.raise_for_status()
        value = response.json()
        prompt_id = value.get("prompt_id") if isinstance(value, dict) else None
        if not isinstance(prompt_id, str):
            raise RuntimeError(f"ComfyUI returned no prompt ID: {value!r}")
        return prompt_id

    def _websocket_url(self, client_id: str) -> str:
        parsed = urlsplit(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/ws?clientId={quote(client_id)}"

    @staticmethod
    def _emit(status_callback: Callable[[str], None] | None, message: str) -> None:
        if status_callback:
            status_callback(message)

    def _wait_for_websocket(
        self,
        connection: websocket.WebSocket,
        prompt_id: str,
        workflow: dict[str, Any],
        *,
        deadline: float,
        status_callback: Callable[[str], None] | None,
    ) -> None:
        last_progress: tuple[str, int, int] | None = None
        while time.monotonic() < deadline:
            connection.settimeout(min(5.0, max(0.1, deadline - time.monotonic())))
            try:
                raw_message = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(raw_message, str):
                continue
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                continue
            event_type = message.get("type")
            data = message.get("data")
            if not isinstance(data, dict):
                continue
            event_prompt_id = data.get("prompt_id")
            if isinstance(event_prompt_id, str) and event_prompt_id != prompt_id:
                continue

            node_id = str(data.get("node", ""))
            node = workflow.get(node_id, {})
            class_type = node.get("class_type", "unknown") if isinstance(node, dict) else "unknown"
            if event_type == "executing" and data.get("node") is not None:
                self._emit(
                    status_callback,
                    f"ComfyUI node {node_id}: {class_type}",
                )
            elif event_type == "progress":
                value = data.get("value")
                maximum = data.get("max")
                if isinstance(value, int) and isinstance(maximum, int) and maximum > 0:
                    progress = (node_id, value, maximum)
                    if progress != last_progress:
                        percentage = round(value * 100 / maximum)
                        self._emit(
                            status_callback,
                            f"ComfyUI progress {node_id} {class_type}: "
                            f"{value}/{maximum} ({percentage}%)",
                        )
                        last_progress = progress
            elif event_type in {"execution_error", "execution_interrupted"}:
                detail = data.get("exception_message") or data
                raise RuntimeError(f"ComfyUI workflow failed: {detail}")
            elif event_type == "execution_success" or (
                event_type == "executing" and data.get("node") is None
            ):
                self._emit(status_callback, "ComfyUI execution completed")
                return
        self._client.post("/interrupt")
        raise TimeoutError(f"ComfyUI workflow {prompt_id} timed out")

    def queue_and_wait(
        self,
        workflow: dict[str, Any],
        *,
        timeout_seconds: int,
        status_callback: Callable[[str], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        client_id = str(uuid.uuid4())
        deadline = time.monotonic() + timeout_seconds
        connection: websocket.WebSocket | None = None
        try:
            connection = self._websocket_factory(
                self._websocket_url(client_id),
                timeout=10,
            )
        except (OSError, websocket.WebSocketException) as error:
            self._emit(
                status_callback,
                f"ComfyUI live progress unavailable; using history polling: {error}",
            )

        try:
            prompt_id = self.queue(workflow, client_id=client_id)
            self._emit(status_callback, f"ComfyUI prompt: {prompt_id}")
            if connection:
                try:
                    self._wait_for_websocket(
                        connection,
                        prompt_id,
                        workflow,
                        deadline=deadline,
                        status_callback=status_callback,
                    )
                except (OSError, websocket.WebSocketException, json.JSONDecodeError) as error:
                    self._emit(
                        status_callback,
                        f"ComfyUI progress stream ended; using history polling: {error}",
                    )
            remaining = max(1, int(deadline - time.monotonic()))
            history = self.wait_for_history(prompt_id, timeout_seconds=remaining)
            return prompt_id, history
        finally:
            if connection:
                connection.close()

    def wait_for_history(
        self, prompt_id: str, *, timeout_seconds: int
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self._client.get(f"/history/{prompt_id}")
            response.raise_for_status()
            value = response.json()
            if isinstance(value, dict) and isinstance(value.get(prompt_id), dict):
                history = value[prompt_id]
                status = history.get("status", {})
                if isinstance(status, dict) and status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI workflow failed: {status!r}")
                return history
            time.sleep(5)
        self._client.post("/interrupt")
        raise TimeoutError(f"ComfyUI workflow {prompt_id} timed out")

    def download_outputs(self, history: dict[str, Any], output_dir: Path) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for item in collect_output_files(history):
            response = self._client.get("/view", params=item)
            response.raise_for_status()
            target = output_dir / Path(item["filename"]).name
            if target.exists():
                target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:8]}{target.suffix}")
            target.write_bytes(response.content)
            written.append(target)
        return written
