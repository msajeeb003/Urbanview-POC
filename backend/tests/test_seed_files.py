"""``core.seeds.upload_sample_files`` with a mocked storage client: one placeholder PDF per sample
document that declares a ``file_key``, nothing for the others, into the private bucket."""

from __future__ import annotations

from core.seeds import upload_sample_files


class FakeStorage:
    def __init__(self) -> None:
        self.bucket_ensured = False
        self.objects: dict[str, tuple[bytes, str]] = {}

    def ensure_bucket(self) -> None:
        self.bucket_ensured = True

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        self.objects[key] = (data, content_type)
        return key


def test_uploads_one_placeholder_pdf_per_stored_sample_document():
    storage = FakeStorage()
    uploaded = upload_sample_files(storage)  # type: ignore[arg-type]
    assert uploaded == 4
    assert storage.bucket_ensured
    assert sorted(storage.objects) == [
        "podgorica/planning-documents/1/document.pdf",
        "podgorica/planning-documents/2/document.pdf",
        "podgorica/planning-documents/4/document.pdf",
        "podgorica/planning-documents/5/document.pdf",
    ]
    for key, (data, content_type) in storage.objects.items():
        assert content_type == "application/pdf", key
        assert data.startswith(b"%PDF-1.4") and data.endswith(b"%%EOF\n"), key
    # page counts follow the seed (document 2 cites up to page 21 and declares 24 pages)
    assert b"/Count 24" in storage.objects["podgorica/planning-documents/2/document.pdf"][0]
