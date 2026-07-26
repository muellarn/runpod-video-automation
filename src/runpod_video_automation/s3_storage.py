from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


_DATACENTER = re.compile(r"^[A-Z]{2,3}-[A-Z]{2}-[0-9]+$")
_PART_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_environment(cls) -> S3Credentials:
        access_key_id = os.environ.get("RUNPOD_S3_ACCESS_KEY_ID", "")
        secret_access_key = os.environ.get("RUNPOD_S3_SECRET_ACCESS_KEY", "")
        missing = []
        if not access_key_id:
            missing.append("RUNPOD_S3_ACCESS_KEY_ID")
        if not secret_access_key:
            missing.append("RUNPOD_S3_SECRET_ACCESS_KEY")
        if missing:
            raise RuntimeError(f"Missing S3 credentials: {', '.join(missing)}")
        return cls(access_key_id, secret_access_key)


def endpoint_for_datacenter(data_center_id: str) -> str:
    if not _DATACENTER.fullmatch(data_center_id):
        raise ValueError(f"Invalid RunPod data center ID: {data_center_id!r}")
    return f"https://s3api-{data_center_id.lower()}.runpod.io/"


class NetworkVolumeStorage:
    def __init__(
        self,
        *,
        volume_id: str,
        data_center_id: str,
        credentials: S3Credentials,
        client: Any | None = None,
    ) -> None:
        self.volume_id = volume_id
        self.data_center_id = data_center_id
        self.endpoint = endpoint_for_datacenter(data_center_id)
        self._client = client or boto3.client(
            "s3",
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            region_name=data_center_id,
            endpoint_url=self.endpoint,
            config=Config(
                retries={"max_attempts": 10, "mode": "standard"},
                read_timeout=300,
            ),
        )

    def object_size(self, key: str) -> int | None:
        try:
            response = self._client.head_object(Bucket=self.volume_id, Key=key)
            return int(response["ContentLength"])
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status not in {403, 404}:
                raise
        response = self._client.list_objects_v2(
            Bucket=self.volume_id,
            Prefix=key,
        )
        for item in response.get("Contents", []):
            if item.get("Key") == key:
                return int(item["Size"])
        return None

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_object(Bucket=self.volume_id, Key=key)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in {403, 404} and self.object_size(key) is None:
                return None
            raise
        value = json.loads(response["Body"].read())
        if not isinstance(value, dict):
            raise RuntimeError(f"S3 object {key!r} is not a JSON object")
        return value

    def object_sha256(self, key: str) -> str:
        response = self._client.get_object(Bucket=self.volume_id, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        try:
            while chunk := body.read(_PART_SIZE):
                digest.update(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return digest.hexdigest()

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        self._client.put_object(
            Bucket=self.volume_id,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    def upload_file(self, key: str, source: Path) -> tuple[int, str]:
        expected_size = source.stat().st_size
        if expected_size <= 0:
            raise ValueError(f"Cannot stage an empty file: {source}")
        upload_id = self.create_multipart_upload(key)
        parts: list[dict[str, object]] = []
        digest = hashlib.sha256()
        uploaded = 0
        try:
            with source.open("rb") as stream:
                part_count = (expected_size + _PART_SIZE - 1) // _PART_SIZE
                for number in range(1, part_count + 1):
                    body = stream.read(_PART_SIZE)
                    if not body:
                        raise RuntimeError(f"Input file changed while staging: {source}")
                    digest.update(body)
                    uploaded += len(body)
                    etag = self.upload_part(key, upload_id, number, body)
                    parts.append({"ETag": etag, "PartNumber": number})
            if uploaded != expected_size or source.stat().st_size != expected_size:
                raise RuntimeError(f"Input file changed while staging: {source}")
            self.complete_multipart_upload(key, upload_id, parts)
        except Exception:
            self.abort_multipart_upload(key, upload_id)
            raise
        return uploaded, digest.hexdigest()

    def download_file(self, key: str, target: Path) -> tuple[int, str]:
        response = self._client.get_object(Bucket=self.volume_id, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with target.open("xb") as stream:
                while chunk := body.read(_PART_SIZE):
                    stream.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return downloaded, digest.hexdigest()

    def create_multipart_upload(self, key: str) -> str:
        response = self._client.create_multipart_upload(
            Bucket=self.volume_id,
            Key=key,
            ContentType="application/octet-stream",
        )
        return str(response["UploadId"])

    def upload_part(
        self, key: str, upload_id: str, number: int, body: bytes
    ) -> str:
        response = self._client.upload_part(
            Bucket=self.volume_id,
            Key=key,
            UploadId=upload_id,
            PartNumber=number,
            Body=body,
        )
        return str(response["ETag"])

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: list[dict[str, object]],
    ) -> None:
        self._client.complete_multipart_upload(
            Bucket=self.volume_id,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(
                Bucket=self.volume_id,
                Key=key,
                UploadId=upload_id,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code != "NoSuchUpload":
                raise
