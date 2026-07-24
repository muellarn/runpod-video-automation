from pathlib import Path
import runpy


Status = runpy.run_path("scripts/watch-progress.py")["Status"]


def test_active_download_overrides_another_completed_download(tmp_path: Path) -> None:
    status = Status()
    status.parse("[#18eb07 5.1GiB/7.4GiB(68%) CN:4 DL:77MiB ETA:31s]")
    status.parse("[#87a07f 5.0GiB/11GiB(43%) CN:4 DL:31MiB ETA:3m38s]")
    status.parse("[#143e33 244MiB/319MiB(76%) CN:4 DL:72MiB ETA:1s]")
    status.parse("143e33|OK  | 69MiB/s|/models/vae/ae.safetensors.part")
    status.parse("[#87a07f 5.0GiB/11GiB(43%) CN:4 DL:31MiB ETA:3m38s]")

    summary = status.summary(running=True, output_dir=tmp_path)

    assert "models: 5.0GiB/11GiB (43%)" in summary
    assert "models: complete" not in summary


def test_downloads_complete_after_all_observed_ids_finish(tmp_path: Path) -> None:
    status = Status()
    status.parse("[#18eb07 7.3GiB/7.4GiB(99%) CN:4 DL:77MiB ETA:1s]")
    status.parse("[#87a07f 10.9GiB/11GiB(99%) CN:4 DL:73MiB ETA:1s]")
    status.parse("18eb07|OK  | 77MiB/s|/models/qwen.safetensors.part")
    status.parse("87a07f|OK  | 73MiB/s|/models/z-image.safetensors.part")

    assert "models: complete" in status.summary(running=True, output_dir=tmp_path)


def test_single_shot_result_counts_as_completed_output(tmp_path: Path) -> None:
    status = Status()
    status.parse("Shot rendered: output/002-follow-up/shot.webm")

    assert status.assembled == "output/002-follow-up/shot.webm"
