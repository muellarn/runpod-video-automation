import json
from pathlib import Path

import pytest

from runpod_video_automation.config import Profile


def test_profile_rejects_parent_model_path(tmp_path: Path) -> None:
    profile = {
        "name": "test",
        "image": "example/image:tag",
        "data_center_id": "EU-RO-1",
        "volume_name": "models",
        "gpu_type_ids": ["GPU"],
        "models": [{"url": "https://example.test/model", "path": "../secret"}],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))

    with pytest.raises(ValueError, match="relative"):
        Profile.load(path)


def test_wan_profile_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    profile = Profile.load(root / "profiles/wan22-i2v-fp8.json")

    assert profile.name == "wan22-i2v-fp8"
    assert len(profile.models) == 4
    assert len(profile.start_image_models) == 3
    assert profile.start_image_models[0].path.endswith(
        "cyberrealisticZImage_v50.safetensors"
    )
    assert profile.start_image_models[0].sha256 == (
        "e48bbd5b7bc496de4e91741639ce8d09d74f2fd308a99be6378208fd2e0707b5"
    )
    assert profile.start_image_models[1].path.endswith("qwen_3_4b.safetensors")
    assert profile.start_image_models[2].path.endswith("ae.safetensors")
    assert all(model.size for model in profile.start_image_models)
    assert all(model.size for model in profile.models)
    assert profile.gpu_type_ids[0] == "NVIDIA H100 80GB HBM3"
    assert profile.max_hourly_cost == 3.5
    assert profile.system_packages == ("gcc", "python3-dev")
    assert profile.comfy_args == ("--enable-triton-backend",)
