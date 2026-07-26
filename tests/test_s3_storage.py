import os
from io import BytesIO
from pathlib import Path

import pytest

from runpod_video_automation.s3_storage import (
    NetworkVolumeStorage,
    S3Credentials,
    endpoint_for_datacenter,
)


def test_s3_credentials_are_read_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_S3_ACCESS_KEY_ID", "user_test")
    monkeypatch.setenv("RUNPOD_S3_SECRET_ACCESS_KEY", "rps_test")

    credentials = S3Credentials.from_environment()

    assert credentials.access_key_id == "user_test"
    assert credentials.secret_access_key == "rps_test"


def test_s3_credentials_report_only_missing_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("RUNPOD_S3_SECRET_ACCESS_KEY", raising=False)

    with pytest.raises(RuntimeError, match="RUNPOD_S3_ACCESS_KEY_ID") as error:
        S3Credentials.from_environment()

    assert "rps_" not in str(error.value)


def test_endpoint_is_derived_from_profile_datacenter() -> None:
    assert endpoint_for_datacenter("US-MO-1") == "https://s3api-us-mo-1.runpod.io/"

    with pytest.raises(ValueError, match="Invalid"):
        endpoint_for_datacenter("../../invalid")


class FakeClient:
    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.completed: dict[str, object] | None = None
        self.aborted = False
        self.download = b""

    def create_multipart_upload(self, **kwargs):
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.parts.append(kwargs["Body"])
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs) -> None:
        self.completed = kwargs

    def abort_multipart_upload(self, **kwargs) -> None:
        self.aborted = True

    def get_object(self, **kwargs):
        return {"Body": BytesIO(self.download)}


def _storage(client: FakeClient) -> NetworkVolumeStorage:
    return NetworkVolumeStorage(
        volume_id="volume-1",
        data_center_id="US-MO-1",
        credentials=S3Credentials("user", "secret"),
        client=client,
    )


def test_storage_streams_file_upload_and_download(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"staged input")
    client = FakeClient()
    storage = _storage(client)

    uploaded = storage.upload_file("runs/input.bin", source)
    client.download = b"rendered output"
    downloaded = storage.download_file("runs/output.bin", target)

    assert b"".join(client.parts) == b"staged input"
    assert client.completed is not None
    assert uploaded[0] == len(b"staged input")
    assert target.read_bytes() == b"rendered output"
    assert downloaded[0] == len(b"rendered output")


def test_storage_aborts_failed_file_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"staged input")
    client = FakeClient()
    storage = _storage(client)
    monkeypatch.setattr(
        storage,
        "upload_part",
        lambda *args: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )

    with pytest.raises(RuntimeError, match="upload failed"):
        storage.upload_file("runs/input.bin", source)

    assert client.aborted is True
