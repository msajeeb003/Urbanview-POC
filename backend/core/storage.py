"""S3-compatible private object storage (MinIO locally, any S3 API in staging/prod).

Object keys are namespaced per municipality: ``{municipality_id}/{kind}/{name}`` where ``kind`` is
``planning-documents``, ``gis``, ``cadastral``, ``reports`` … so one bucket can hold several
municipalities without a rebuild (BRD §8). Buckets are private; hand out presigned URLs.

boto3 is synchronous: call these from Celery tasks, or wrap with
``starlette.concurrency.run_in_threadpool`` inside request handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from core.config import Settings

log = logging.getLogger("urbanview.storage")


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=(
                settings.s3_access_key.get_secret_value() if settings.s3_access_key else None
            ),
            aws_secret_access_key=(
                settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None
            ),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_use_path_style else "virtual"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @staticmethod
    def object_key(municipality_id: str, kind: str, name: str) -> str:
        return f"{municipality_id}/{kind}/{name.lstrip('/')}"

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            log.info("creating bucket %s", self.bucket)
            self.client.create_bucket(Bucket=self.bucket)

    def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def presigned_get_url(self, key: str, expires_in: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
