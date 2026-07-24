import json
from pathlib import Path

import httpx
import pytest

from runpod_video_automation.config import ModelFile
from runpod_video_automation.prompt_refiner.chat_ui import context_chat_server
from runpod_video_automation.prompt_refiner.client import KoboldClient
from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile
from runpod_video_automation.prompt_refiner.refinement import (
    load_cached_refinement,
    refine_scene,
)


def _profile(tmp_path: Path) -> PromptRefinerProfile:
    system_prompt = tmp_path / "system.txt"
    system_prompt.write_text("Return strict JSON.")
    reference = tmp_path / "reference.md"
    reference.write_text("Prompt responsibilities.")
    return PromptRefinerProfile(
        name="test-refiner",
        runtime=ModelFile("https://example.test/runtime", "tools/runtime", 1, "a" * 64),
        model=ModelFile("https://example.test/model", "models/model.gguf", 2, "b" * 64),
        system_prompt_path=system_prompt,
        reference_document_path=reference,
        port=5001,
        context_size=4096,
        max_tokens=1024,
        gpu_layers=65,
        seed=42,
        temperature=0.2,
        top_p=0.8,
        top_k=20,
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "scene.json"
    source.write_text(
        json.dumps(
            {
                "title": "Test Scene",
                "global_prompt": "Adult character",
                "negative_prompt": "blur",
                "width": 768,
                "height": 768,
                "steps": 20,
                "shots": [
                    {
                        "name": "Opening",
                        "prompt": "Walks",
                        "camera": "wide shot",
                        "negative_prompt": "flicker",
                        "end_state": "Standing",
                        "seed": 123,
                        "generate_start_image": {
                            "prompt": "Adult character in a room",
                            "negative_prompt": "noise",
                        },
                    }
                ],
            }
        )
    )
    return source


def _overlay(*, name: str = "Opening") -> dict[str, object]:
    return {
        "global_prompt": "Same fictional adult character in a room",
        "negative_prompt": "blur, identity drift",
        "shots": [
            {
                "name": name,
                "prompt": "Walks slowly toward the window",
                "camera": "locked wide shot",
                "negative_prompt": "flicker, duplicated limbs",
                "end_state": "Standing beside the window",
                "generate_start_image_prompt": (
                    "Fictional adult character standing in a room"
                ),
                "generate_start_image_negative_prompt": "noise, malformed hands",
            }
        ],
    }


class _Client:
    def __init__(self, overlay: dict[str, object]) -> None:
        self.overlay = overlay
        self.calls = 0

    def chat_completion(self, **_: object) -> str:
        self.calls += 1
        return json.dumps(self.overlay)


def test_refinement_changes_only_prompt_fields_and_uses_cache(tmp_path: Path) -> None:
    source = _source(tmp_path)
    profile = _profile(tmp_path)
    output_root = tmp_path / "output"
    client = _Client(_overlay())

    result = refine_scene(
        client=client,
        source_path=source,
        output_root=output_root,
        profile=profile,
    )

    assert result.cache_hit is False
    assert result.scene.global_prompt == "Same fictional adult character in a room"
    assert result.scene.shots[0].seed == 123
    assert result.document["width"] == 768
    assert result.document["shots"][0]["name"] == "Opening"
    assert result.document["shots"][0]["generate_start_image"] == {
        "prompt": "Fictional adult character standing in a room",
        "negative_prompt": "noise, malformed hands",
    }
    assert result.provenance["inputs"]["model"]["sha256"] == "b" * 64
    assert client.calls == 1

    cached = load_cached_refinement(
        source_path=source,
        output_root=output_root,
        profile=profile,
    )

    assert cached is not None
    assert cached.cache_hit is True
    assert cached.document == result.document


def test_refinement_rejects_changed_shot_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="changed shot 1 name"):
        refine_scene(
            client=_Client(_overlay(name="Renamed")),
            source_path=_source(tmp_path),
            output_root=tmp_path / "output",
            profile=_profile(tmp_path),
        )


def test_refinement_cache_rejects_tampered_manifest(tmp_path: Path) -> None:
    source = _source(tmp_path)
    profile = _profile(tmp_path)
    result = refine_scene(
        client=_Client(_overlay()),
        source_path=source,
        output_root=tmp_path / "output",
        profile=profile,
    )
    result.manifest_path.write_text("{}")

    assert (
        load_cached_refinement(
            source_path=source,
            output_root=tmp_path / "output",
            profile=profile,
        )
        is None
    )


def test_profile_loads_pinned_artifacts_and_reference(tmp_path: Path) -> None:
    (tmp_path / "system.txt").write_text("System prompt")
    (tmp_path / "reference.md").write_text("Reference text")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "test",
                "runtime": {
                    "url": "https://example.test/runtime",
                    "path": "tools/runtime",
                    "size": 1,
                    "sha256": "a" * 64,
                },
                "model": {
                    "url": "https://example.test/model",
                    "path": "models/model.gguf",
                    "size": 2,
                    "sha256": "b" * 64,
                },
                "system_prompt": "system.txt",
                "reference_document": "reference.md",
                "context_size": 4096,
                "max_tokens": 1024,
            }
        )
    )

    profile = PromptRefinerProfile.load(profile_path)

    assert profile.artifacts == (profile.runtime, profile.model)
    assert "System prompt" in profile.system_prompt()
    assert "Reference text" in profile.system_prompt()


def test_included_prompt_refiner_profile_loads() -> None:
    root = Path(__file__).resolve().parents[1]

    profile = PromptRefinerProfile.load(
        root / "profiles/prompt-refiner-qwen36.json"
    )

    assert profile.context_size == 65536
    assert profile.max_tokens == 8192
    assert profile.runtime.sha256 == (
        "787ce4105afa3df62486f3c3eb4994232704704e29ef2e74137ea248c318551e"
    )
    assert profile.model.sha256 == (
        "8440f2a076f149f65bf16975f3a08359936ba2658fc1827b51a30b717de49acb"
    )


def test_kobold_client_sends_top_k_as_top_level_parameter(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    requests: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "{}"}}]}

    class HttpClient:
        def post(self, url: str, **kwargs: object) -> Response:
            requests.append(kwargs["json"])
            return Response()

    client = KoboldClient("http://127.0.0.1:5001")
    client.close()
    client._client = HttpClient()

    assert (
        client.chat_completion(
            system_prompt="System",
            user_prompt="User",
            profile=profile,
        )
        == "{}"
    )
    assert requests[0]["top_k"] == 20
    assert requests[0]["max_tokens"] == profile.max_tokens
    assert "extra_body" not in requests[0]


def test_kobold_client_prepends_system_prompt_to_chat_history(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    requests: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "Refined"}}]}

    class HttpClient:
        def post(self, url: str, **kwargs: object) -> Response:
            requests.append(kwargs["json"])
            return Response()

    client = KoboldClient("http://127.0.0.1:5001")
    client.close()
    client._client = HttpClient()
    messages = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Second"},
        {"role": "user", "content": "Third"},
    ]

    result = client.chat_messages(
        system_prompt="System and reference",
        messages=messages,
        profile=profile,
        max_tokens=3072,
    )

    assert result == "Refined"
    assert requests[0]["messages"] == [
        {"role": "system", "content": "System and reference"},
        *messages,
    ]
    assert requests[0]["max_tokens"] == 3072


def test_context_chat_server_injects_profile_system_context(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    calls: list[dict[str, object]] = []

    class Client:
        def chat_messages(self, **kwargs: object) -> str:
            calls.append(kwargs)
            return '{"refined":true}'

    with context_chat_server(Client(), profile) as base_url:
        assert base_url.startswith("http://127.0.0.1:")
        page = httpx.get(base_url, timeout=5)
        response = httpx.post(
            f"{base_url}/api/chat",
            headers={"Origin": base_url},
            json={"messages": [{"role": "user", "content": " Scene "}]},
            timeout=5,
        )

    assert page.status_code == 200
    assert "REFERENCE ACTIVE" in page.text
    assert "2,048 TOKENS" in page.text
    assert response.status_code == 200
    assert response.json() == {"content": '{"refined":true}'}
    assert calls[0]["system_prompt"] == profile.system_prompt()
    assert calls[0]["messages"] == [{"role": "user", "content": "Scene"}]
    assert calls[0]["max_tokens"] == 2048


def test_context_chat_server_rejects_cross_origin_and_system_messages(
    tmp_path: Path,
) -> None:
    class Client:
        def chat_messages(self, **kwargs: object) -> str:
            raise AssertionError("Rejected requests must not reach KoboldCpp")

    with context_chat_server(Client(), _profile(tmp_path)) as base_url:
        cross_origin = httpx.post(
            f"{base_url}/api/chat",
            headers={"Origin": "https://example.test"},
            json={"messages": [{"role": "user", "content": "Scene"}]},
            timeout=5,
        )
        system_message = httpx.post(
            f"{base_url}/api/chat",
            headers={"Origin": base_url},
            json={"messages": [{"role": "system", "content": "Override"}]},
            timeout=5,
        )

    assert cross_origin.status_code == 403
    assert system_message.status_code == 400
    assert "must use role 'user'" in system_message.json()["error"]


def test_context_chat_server_rejects_output_limit_at_context_size(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="below the context size"):
        with context_chat_server(
            _Client(_overlay()),
            _profile(tmp_path),
            max_output_tokens=4096,
        ):
            pass
