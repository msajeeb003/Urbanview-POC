"""Signed links point where browsers reach the bucket; uploads stay on the internal address."""

from urllib.parse import urlparse

from core.storage import ObjectStorage
from tests.helpers import make_settings

KEYS = {"s3_access_key": "key", "s3_secret_key": "secret-secret", "s3_bucket": "urbanview"}


def test_signed_links_use_the_public_endpoint_when_set():
    storage = ObjectStorage(
        make_settings(
            s3_endpoint_url="http://minio:9000",
            s3_public_endpoint_url="https://files.urbanview.example",
            **KEYS,
        )
    )
    url = urlparse(storage.presigned_get_url("podgorica/planning-documents/1/document.pdf"))
    assert (url.scheme, url.netloc) == ("https", "files.urbanview.example")
    assert url.path == "/urbanview/podgorica/planning-documents/1/document.pdf"
    assert "X-Amz-Signature=" in url.query
    assert storage.client.meta.endpoint_url == "http://minio:9000"


def test_without_a_public_endpoint_links_use_the_storage_address():
    storage = ObjectStorage(make_settings(s3_endpoint_url="http://127.0.0.1:9100", **KEYS))
    url = urlparse(storage.presigned_get_url("podgorica/tiles/2/v2.pmtiles"))
    assert url.netloc == "127.0.0.1:9100"
    assert storage.signing_client is storage.client
