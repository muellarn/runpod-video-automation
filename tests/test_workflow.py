from pathlib import Path

import pytest

from runpod_video_automation.workflow import (
    apply_overrides,
    collect_output_files,
    load_workflow,
)


def test_included_wan_workflow_is_api_format() -> None:
    root = Path(__file__).resolve().parents[1]

    workflow = load_workflow(root / "workflows/wan22-i2v-14b-api.json")

    assert workflow["50"]["class_type"] == "WanImageToVideo"
    assert workflow["52"]["inputs"]["image"] == "input.png"
    class_types = {node["class_type"] for node in workflow.values()}
    assert "SaveWEBM" in class_types
    assert "SaveAnimatedWEBP" not in class_types


def test_apply_overrides_parses_json_and_plain_text() -> None:
    workflow = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
        "9": {"class_type": "KSampler", "inputs": {"seed": 1}},
    }

    apply_overrides(workflow, ["6.text=new prompt", "9.seed=42"])

    assert workflow["6"]["inputs"]["text"] == "new prompt"
    assert workflow["9"]["inputs"]["seed"] == 42


def test_apply_overrides_rejects_unknown_input() -> None:
    workflow = {"6": {"class_type": "Node", "inputs": {}}}

    with pytest.raises(ValueError, match="no input"):
        apply_overrides(workflow, ["6.text=hello"])


def test_collect_output_files_handles_video_audio_and_images() -> None:
    history = {
        "outputs": {
            "1": {"images": [{"filename": "preview.png", "type": "temp"}]},
            "2": {"gifs": [{"filename": "movie.mp4", "subfolder": "video", "type": "output"}]},
            "3": {"audio": [{"filename": "sound.flac", "type": "output"}]},
        }
    }

    files = collect_output_files(history)

    assert [item["filename"] for item in files] == ["movie.mp4", "sound.flac"]
