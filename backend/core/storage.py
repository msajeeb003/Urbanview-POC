"""S3-compatible private object storage (MinIO locally, any S3 API in staging/prod).

Object keys are namespaced per municipality: ``{municipality_id}/{kind}/{name}`` where ``kind`` is
``planning-documents``, ``gis``, ``cadastral``, ``reports`` … so one bucket can hold several
municipalities without a rebuild (BRD §8). Buckets are private; hand out presigned URLs. Those
are signed for ``S3_PUBLIC_ENDPOINT_URL`` when set (the address browsers use; signing is local, no
request), while uploads and reads go to ``S3_ENDPOINT_URL`` (e.g. the storage container on the
same network).

Planning documents live under ``{municipality_id}/planning-documents/{document_id}/``: the PDF at
``planning_documents.file_key`` (``document_key``) and rendered pages at ``pages/NNNN.png``
(``page_image_key``); the source viewer signs those keys, nothing else leaves the API.

boto3 is synchronous: call these from Celery tasks, or wrap with
``starlette.concurrency.run_in_threadpool`` inside request handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from core.config import Settings

log = logging.getLogger("urbanview.storage")

PLANNING_DOCUMENTS_KIND = "planning-documents"


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.client = self._client(settings, settings.s3_endpoint_url)
        public = settings.s3_public_endpoint_url
        self.signing_client = (
            self._client(settings, public)
            if public and public != settings.s3_endpoint_url
            else self.client
        )

    @staticmethod
    def _client(settings: Settings, endpoint_url: str | None) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
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
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    @staticmethod
    def object_key(municipality_id: str, kind: str, name: str) -> str:
        return f"{municipality_id}/{kind}/{name.lstrip('/')}"

    @staticmethod
    def document_key(municipality_id: str, document_id: int, filename: str = "document.pdf") -> str:
        return f"{municipality_id}/{PLANNING_DOCUMENTS_KIND}/{document_id}/{filename}"

    @staticmethod
    def page_image_key(municipality_id: str, document_id: int, page: int) -> str:
        """Rendered page image (PNG) of a stored planning document; 1-based, zero-padded page."""
        return f"{municipality_id}/{PLANNING_DOCUMENTS_KIND}/{document_id}/pages/{page:04d}.png"

    @staticmethod
    def upload_key(municipality_id: str, kind: str, sha256: str, filename: str) -> str:
        """Staff uploads, addressed by content hash so identical files share one object."""
        return f"{municipality_id}/uploads/{kind}/{sha256}/{filename}"

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

    def put_file(self, key: str, path: Any, content_type: str = "application/octet-stream") -> str:
        """Streamed upload of a local file (multipart for large archives)."""
        self.client.upload_file(
            str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def presigned_get_url(
        self,
        key: str,
        expires_in: int = 900,
        *,
        content_type: str | None = None,
        inline: bool = False,
    ) -> str:
        """Signed GET URL. ``content_type`` / ``inline`` are response-header overrides baked into
        the signature, so a browser renders the page instead of downloading it."""
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        if content_type:
            params["ResponseContentType"] = content_type
        if inline:
            params["ResponseContentDisposition"] = "inline"
        return self.signing_client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_in
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
