import json
from pathlib import Path

import pytest

from runpod_video_automation.config import Profile


def _profile(**overrides: object) -> dict[str, object]:
    profile: dict[str, object] = {
        "name": "test",
        "image": "example/image:tag",
        "data_center_id": "EU-RO-1",
        "volume_name": "models",
        "gpu_type_ids": ["GPU"],
        "model_groups": {},
        "workflows": {},
    }
    profile.update(overrides)
    return profile


def _load(tmp_path: Path, profile: dict[str, object]) -> Profile:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    return Profile.load(path)


def test_profile_rejects_parent_model_path(tmp_path: Path) -> None:
    profile = _profile(
        model_groups={
            "video": [{"url": "https://example.test/model", "path": "../secret"}]
        }
    )

    with pytest.raises(ValueError, match="relative"):
        _load(tmp_path, profile)


def test_profile_rejects_legacy_model_fields(tmp_path: Path) -> None:
    profile = _profile(models=[])

    with pytest.raises(ValueError, match="Legacy profile fields"):
        _load(tmp_path, profile)


def test_profile_selects_and_deduplicates_arbitrary_model_groups(
    tmp_path: Path,
) -> None:
    shared = {"url": "https://example.test/shared", "path": "models/shared", "size": 1}
    profile = _load(
        tmp_path,
        _profile(
            model_groups={"video": [shared], "image": [shared]},
            workflows={
                "video": {
                    "path": "video.json",
                    "adapter": "custom_video",
                    "model_groups": ["video", "image"],
                }
            },
        ),
    )

    selection = profile.select_workflow("video")

    assert selection.adapter == "custom_video"
    assert selection.path == (tmp_path / "video.json").resolve()
    assert selection.model_groups == ("video", "image")
    assert len(selection.models) == 1


def test_profile_rejects_conflicting_model_destinations(tmp_path: Path) -> None:
    profile = _profile(
        model_groups={
            "one": [{"url": "https://example.test/one", "path": "models/shared"}],
            "two": [{"url": "https://example.test/two", "path": "models/shared"}],
        }
    )

    with pytest.raises(ValueError, match="Conflicting model definitions"):
        _load(tmp_path, profile)


def test_profile_rejects_alias_target_colliding_with_model(tmp_path: Path) -> None:
    profile = _profile(
        model_groups={
            "all": [
                {
                    "url": "https://example.test/modern",
                    "path": "models/diffusion_models/shared",
                },
                {
                    "url": "https://example.test/legacy",
                    "path": "models/unet/shared",
                },
            ]
        },
        model_path_aliases=[
            {"source": "models/diffusion_models", "target": "models/unet"}
        ],
    )

    with pytest.raises(ValueError, match="alias target.*collides"):
        _load(tmp_path, profile)


def test_profile_rejects_nested_model_file_paths(tmp_path: Path) -> None:
    profile = _profile(
        model_groups={
            "all": [
                {"url": "https://example.test/one", "path": "models/model"},
                {"url": "https://example.test/two", "path": "models/model/child"},
            ]
        }
    )

    with pytest.raises(ValueError, match="nested below model file"):
        _load(tmp_path, profile)


def test_profile_rejects_unknown_workflow_model_group(tmp_path: Path) -> None:
    profile = _profile(
        workflows={
            "video": {
                "path": "video.json",
                "adapter": "custom",
                "model_groups": ["missing"],
            }
        }
    )

    with pytest.raises(ValueError, match="unknown model groups"):
        _load(tmp_path, profile)


def test_adapter_override_drops_incompatible_preset_defaults(tmp_path: Path) -> None:
    profile = _load(
        tmp_path,
        _profile(
            workflows={
                "start_image": {
                    "path": "start.json",
                    "adapter": "z_image_turbo",
                    "model_groups": [],
                    "defaults": {"checkpoint": "z-image.safetensors"},
                }
            }
        ),
    )

    selection = profile.select_workflow("start_image", adapter="sdxl")

    assert selection.adapter == "sdxl"
    assert selection.defaults == {}


def test_profile_uses_explicit_setup_model_groups(tmp_path: Path) -> None:
    profile = _load(
        tmp_path,
        _profile(
            model_groups={"default": [], "optional": []},
            setup_model_groups=["default"],
            workflows={
                "video": {
                    "path": "video.json",
                    "adapter": "video",
                    "model_groups": ["default"],
                },
                "image_edit": {
                    "path": "edit.json",
                    "adapter": "edit",
                    "model_groups": ["optional"],
                },
            },
        ),
    )

    assert profile.default_model_groups == ("default",)


def test_profile_rejects_unknown_setup_model_group(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="setup_model_groups.*unknown"):
        _load(
            tmp_path,
            _profile(
                model_groups={"default": []},
                setup_model_groups=["missing"],
            ),
        )


def test_wan_profile_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    profile = Profile.load(root / "profiles/wan22-i2v-fp8.json")
    video = profile.select_workflow("video")
    start_image = profile.select_workflow("start_image")
    image_edit = profile.select_workflow("image_edit")

    assert len(video.models) == 4
    assert len(start_image.models) == 3
    assert len(image_edit.models) == 3
    assert start_image.models[0].path.endswith(
        "cyberrealisticZImage_v50.safetensors"
    )
    assert start_image.models[0].sha256 == (
        "e48bbd5b7bc496de4e91741639ce8d09d74f2fd308a99be6378208fd2e0707b5"
    )
    assert all(model.size for model in (*video.models, *start_image.models))
    assert video.adapter == "wan22_i2v"
    assert start_image.adapter == "z_image_turbo"
    assert image_edit.adapter == "qwen_image_edit_2511"
    assert profile.default_model_groups == ("wan22-i2v", "z-image-turbo")
    assert profile.gpu_type_ids[0] == "NVIDIA H100 80GB HBM3"
    assert profile.max_hourly_cost == 3.5
    assert profile.system_packages == ("gcc", "python3-dev")
    assert profile.comfy_args == ("--enable-triton-backend",)
