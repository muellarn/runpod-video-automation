from __future__ import annotations

import time
from typing import Any

import httpx

from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile


class KoboldClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=120.0)

    def close(self) -> None:
        self._client.close()

    def wait_until_ready(self, timeout_seconds: int = 600) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_error = "no response"
        url = f"{self.base_url}/api/v1/info/version"
        while time.monotonic() < deadline:
            try:
                response = self._client.get(url)
                response.raise_for_status()
                value = response.json()
                if isinstance(value, dict):
                    return value
                last_error = "readiness response was not an object"
            except (httpx.HTTPError, ValueError) as error:
                last_error = str(error)
            time.sleep(2)
        raise TimeoutError(f"KoboldCpp did not become ready: {last_error}")

    def chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        profile: PromptRefinerProfile,
    ) -> str:
        return self.chat_messages(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            profile=profile,
        )

    def chat_messages(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        profile: PromptRefinerProfile,
        max_tokens: int | None = None,
    ) -> str:
        output_tokens = profile.max_tokens if max_tokens is None else max_tokens
        if not 0 < output_tokens < profile.context_size:
            raise ValueError("Output token limit must be below the context size")
        response = self._client.post(
            f"{self.base_url}/v1/chat/completions",
            json={
                "model": profile.name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *messages,
                ],
                "max_tokens": output_tokens,
                "temperature": profile.temperature,
                "top_p": profile.top_p,
                "top_k": profile.top_k,
                "seed": profile.seed,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=30 * 60,
        )
        response.raise_for_status()
        value = response.json()
        try:
            content = value["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("KoboldCpp returned an invalid chat response") from error
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("KoboldCpp returned empty chat content")
        return content.strip()
