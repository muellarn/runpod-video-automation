from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from runpod_video_automation.config import ModelFile


@dataclass(frozen=True)
class PromptRefinerProfile:
    name: str
    runtime: ModelFile
    model: ModelFile
    system_prompt_path: Path
    reference_document_path: Path | None
    port: int
    context_size: int
    max_tokens: int
    gpu_layers: int
    seed: int
    temperature: float
    top_p: float
    top_k: int

    @classmethod
    def load(cls, path: Path) -> PromptRefinerProfile:
        path = path.expanduser().resolve()
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Prompt refiner profile must be a JSON object")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Prompt refiner profile requires a non-empty name")
        raw_runtime = data.get("runtime")
        raw_model = data.get("model")
        if not isinstance(raw_runtime, dict) or not isinstance(raw_model, dict):
            raise ValueError(
                "Prompt refiner profile requires runtime and model objects"
            )
        runtime = ModelFile.from_dict(raw_runtime)
        model = ModelFile.from_dict(raw_model)
        for label, artifact in (("runtime", runtime), ("model", model)):
            if artifact.size is None or artifact.sha256 is None:
                raise ValueError(
                    f"Prompt refiner {label} requires pinned size and SHA-256"
                )
        raw_prompt_path = data.get("system_prompt")
        if not isinstance(raw_prompt_path, str) or not raw_prompt_path:
            raise ValueError("Prompt refiner profile requires system_prompt")
        system_prompt_path = Path(raw_prompt_path).expanduser()
        if not system_prompt_path.is_absolute():
            system_prompt_path = path.parent / system_prompt_path
        system_prompt_path = system_prompt_path.resolve()
        if not system_prompt_path.is_file():
            raise ValueError(
                f"Prompt refiner system prompt not found: {system_prompt_path}"
            )
        raw_reference_path = data.get("reference_document")
        reference_document_path: Path | None = None
        if raw_reference_path is not None:
            if not isinstance(raw_reference_path, str) or not raw_reference_path:
                raise ValueError("Prompt refiner reference_document must be a path")
            reference_document_path = Path(raw_reference_path).expanduser()
            if not reference_document_path.is_absolute():
                reference_document_path = path.parent / reference_document_path
            reference_document_path = reference_document_path.resolve()
            if not reference_document_path.is_file():
                raise ValueError(
                    "Prompt refiner reference document not found: "
                    f"{reference_document_path}"
                )

        port = int(data.get("port", 5001))
        context_size = int(data.get("context_size", 32768))
        max_tokens = int(data.get("max_tokens", 8192))
        gpu_layers = int(data.get("gpu_layers", 65))
        seed = int(data.get("seed", 3407))
        temperature = float(data.get("temperature", 0.2))
        top_p = float(data.get("top_p", 0.8))
        top_k = int(data.get("top_k", 20))
        if not 1024 <= port <= 65535:
            raise ValueError("Prompt refiner port must be between 1024 and 65535")
        if context_size <= 0 or max_tokens <= 0 or max_tokens >= context_size:
            raise ValueError(
                "Prompt refiner context_size must exceed positive max_tokens"
            )
        if gpu_layers < -1:
            raise ValueError("Prompt refiner gpu_layers must be -1 or greater")
        if not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("Prompt refiner seed is out of range")
        if not 0 <= temperature <= 2:
            raise ValueError("Prompt refiner temperature must be between 0 and 2")
        if not 0 < top_p <= 1:
            raise ValueError("Prompt refiner top_p must be between 0 and 1")
        if top_k <= 0:
            raise ValueError("Prompt refiner top_k must be positive")
        return cls(
            name=name,
            runtime=runtime,
            model=model,
            system_prompt_path=system_prompt_path,
            reference_document_path=reference_document_path,
            port=port,
            context_size=context_size,
            max_tokens=max_tokens,
            gpu_layers=gpu_layers,
            seed=seed,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
        )

    @property
    def artifacts(self) -> tuple[ModelFile, ...]:
        return self.runtime, self.model

    @property
    def remote_runtime_path(self) -> str:
        return f"/runpod-volume/{self.runtime.path}"

    @property
    def remote_model_path(self) -> str:
        return f"/runpod-volume/{self.model.path}"

    def generation_settings(self) -> dict[str, int | float]:
        return {
            "context_size": self.context_size,
            "max_tokens": self.max_tokens,
            "gpu_layers": self.gpu_layers,
            "seed": self.seed,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }

    def system_prompt(self) -> str:
        prompt = self.system_prompt_path.read_text().strip()
        if self.reference_document_path is None:
            return prompt
        reference = self.reference_document_path.read_text().strip()
        return (
            f"{prompt}\n\n"
            "The following document is the authoritative scene-manifest and "
            "prompting reference. Use its prompt-layer responsibilities and "
            "model-specific guidance, but still return only the required JSON "
            "overlay.\n\n"
            f"<scene_manifest_reference>\n{reference}\n"
            "</scene_manifest_reference>"
        )
