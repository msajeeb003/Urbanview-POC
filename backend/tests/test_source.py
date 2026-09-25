"""``GET /v1/source/...`` with a mocked storage client and an in-memory repository: only signed
URLs leave the API, page image vs PDF anchor, configurable expiry, the value's box, and 404 only
for what truly does not exist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import NoCredentialsError

from api.services.source import DocumentRow, ValueRow
from tests.helpers import make_app, make_client, make_redis, make_settings

REGISTRY = "https://lamp.gov.me/PlanningDocument?m=PG"
DOC_IMAGES = DocumentRow(
    id=2,
    name="DUP Centar – Zona C2",
    status="adopted",
    registry_url=REGISTRY,
    file_key="podgorica/planning-documents/2/document.pdf",
    page_count=24,
    page_images_rendered=True,
)
DOC_PDF_ONLY = DocumentRow(
    id=4,
    name="DUP Stari Aerodrom",
    status="adopted",
    registry_url=None,
    file_key="podgorica/planning-documents/4/original-scan.pdf",
    page_count=16,
    page_images_rendered=False,
)
DOC_NOT_STORED = DocumentRow(
    id=3,
    name="Izmjene i dopune DUP-a Centar 2023",
    status="in_progress",
    registry_url=REGISTRY,
    file_key=None,
    page_count=None,
    page_images_rendered=False,
)
DOC_UNKNOWN_PAGES = DocumentRow(
    id=5,
    name="DUP Stari Aerodrom (2009)",
    status="superseded",
    registry_url=None,
    file_key="podgorica/planning-documents/5/document.pdf",
    page_count=None,
    page_images_rendered=True,
)
VALUE_NUMBER = ValueRow(
    id=1,
    field_key="max_far",
    label_en="Maximum floor area ratio",
    label_me="Maksimalni indeks izgrađenosti",
    value_text=None,
    value_number=3.2,
    unit=None,
    urban_parcel_id=1,
    source_page=12,
    source_bbox=[72, 410, 520, 428],
    source_note="table 3 – UP 12",
    document=DOC_IMAGES,
)
VALUE_TEXT = ValueRow(
    id=2,
    field_key="land_use",
    label_en="Land use",
    label_me="Namjena",
    value_text="Residential",
    value_number=None,
    unit=None,
    urban_parcel_id=None,
    source_page=3,
    source_bbox=None,
    source_note=None,
    document=DOC_PDF_ONLY,
)


class FakeRepository:
    def __init__(self, documents=(), values=()) -> None:
        self.documents = {d.id: d for d in documents}
        self.values = {v.id: v for v in values}

    async def get_document(self, document_id):
        return self.documents.get(document_id)

    async def get_value(self, value_id):
        return self.values.get(value_id)


class FakeStorage:
    """Signs like the real client would, with no bucket, network or credentials."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def presigned_get_url(self, key, expires_in=900, *, content_type=None, inline=False):
        self.calls.append(
            {"key": key, "expires_in": expires_in, "content_type": content_type, "inline": inline}
        )
        if self.error is not None:
            raise self.error
        return (
            f"https://minio.test/urbanview-dev/{key}?X-Amz-Expires={expires_in}&X-Amz-Signature=sig"
        )


def build(*, storage=None, repository=None, **overrides):
    settings = make_settings(rate_limit_requests=100, **overrides)
    if repository is None:
        repository = FakeRepository(
            [DOC_IMAGES, DOC_PDF_ONLY, DOC_NOT_STORED, DOC_UNKNOWN_PAGES],
            [VALUE_NUMBER, VALUE_TEXT],
        )
    return make_app(
        settings, make_redis(), storage=storage or FakeStorage(), source_repository=repository
    )


async def get(app, path):
    async with app.router.lifespan_context(app), make_client(app) as client:
        return await client.get(path)


def _remaining(body: dict) -> timedelta:
    return datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)


async def test_rendered_page_is_a_signed_image_url():
    storage = FakeStorage()
    r = await get(build(storage=storage), "/v1/source/2/page/12")
    assert r.status_code == 200, r.text
    assert r.headers["Cache-Control"] == "no-store"
    body = r.json()
    assert body["kind"] == "page_image"
    assert body["content_type"] == "image/png"
    assert body["url"] == (
        "https://minio.test/urbanview-dev/podgorica/planning-documents/2/pages/0012.png"
        "?X-Amz-Expires=900&X-Amz-Signature=sig"
    )
    assert (body["document_id"], body["page"], body["page_count"]) == (2, 12, 24)
    assert body["document_name"] == "DUP Centar – Zona C2"
    assert body["document_status"] == "adopted"
    assert body["registry_url"] == REGISTRY
    assert body["value"] is None
    assert body["expires_in_seconds"] == 900
    assert timedelta(seconds=880) < _remaining(body) <= timedelta(seconds=900)
    assert storage.calls == [
        {
            "key": "podgorica/planning-documents/2/pages/0012.png",
            "expires_in": 900,
            "content_type": "image/png",
            "inline": True,
        }
    ]
    # nothing about storage leaves the API except the signed URL
    assert "X-Amz-Signature" in body["url"]
    assert not {"file_key", "key", "bucket"} & set(body)


async def test_without_page_images_the_pdf_is_served_with_a_page_anchor():
    storage = FakeStorage()
    r = await get(build(storage=storage), "/v1/source/4/page/7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf_page"
    assert body["content_type"] == "application/pdf"
    assert body["url"] == (
        "https://minio.test/urbanview-dev/podgorica/planning-documents/4/original-scan.pdf"
        "?X-Amz-Expires=900&X-Amz-Signature=sig#page=7"
    )
    assert (body["document_id"], body["page"], body["page_count"]) == (4, 7, 16)
    assert body["registry_url"] is None
    assert storage.calls[0]["key"] == "podgorica/planning-documents/4/original-scan.pdf"
    assert storage.calls[0]["content_type"] == "application/pdf"


async def test_an_unknown_page_count_serves_the_pdf_even_when_images_are_flagged():
    r = await get(build(), "/v1/source/5/page/40")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf_page"
    assert body["page_count"] is None
    assert body["url"].endswith("/5/document.pdf?X-Amz-Expires=900&X-Amz-Signature=sig#page=40")
    assert body["document_status"] == "superseded"


async def test_expiry_is_configuration():
    storage = FakeStorage()
    r = await get(build(storage=storage, source_url_expires_seconds=120), "/v1/source/2/page/1")
    body = r.json()
    assert body["expires_in_seconds"] == 120
    assert storage.calls[0]["expires_in"] == 120
    assert "X-Amz-Expires=120" in body["url"]
    assert timedelta(seconds=100) < _remaining(body) <= timedelta(seconds=120)


async def test_value_route_adds_the_box_on_the_cited_page():
    r = await get(build(), "/v1/source/value/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["document_id"], body["page"], body["kind"]) == (2, 12, "page_image")
    assert body["url"].endswith("/2/pages/0012.png?X-Amz-Expires=900&X-Amz-Signature=sig")
    assert body["value"] == {
        "value_id": 1,
        "field_key": "max_far",
        "label_en": "Maximum floor area ratio",
        "label_me": "Maksimalni indeks izgrađenosti",
        "value": 3.2,
        "unit": None,
        "urban_parcel_id": 1,
        "bbox": [72, 410, 520, 428],
        "bbox_space": "pdf-points-bottom-left",
        "note": "table 3 – UP 12",
    }


async def test_value_route_for_a_document_level_text_value_without_a_box():
    r = await get(build(), "/v1/source/value/2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["document_id"], body["page"], body["kind"]) == (4, 3, "pdf_page")
    assert body["url"].endswith("#page=3")
    assert body["value"]["value"] == "Residential"
    assert body["value"]["urban_parcel_id"] is None
    assert body["value"]["bbox"] is None and body["value"]["note"] is None


@pytest.mark.parametrize(
    ("path", "details"),
    [
        ("/v1/source/999/page/1", {"document_id": 999}),
        ("/v1/source/2/page/25", {"document_id": 2, "page": 25, "page_count": 24}),
        ("/v1/source/3/page/1", {"document_id": 3, "page": 1, "reason": "not_stored"}),
        ("/v1/source/value/999", {"value_id": 999}),
    ],
    ids=["unknown document", "page beyond the document", "document not stored", "unknown value"],
)
async def test_only_what_truly_does_not_exist_is_404(path, details):
    storage = FakeStorage()
    r = await get(build(storage=storage), path)
    assert r.status_code == 404, r.text
    body = r.json()["error"]
    assert body["code"] == "not_found"
    assert body["details"] == details
    assert storage.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/source/2/page/0",
        "/v1/source/0/page/1",
        "/v1/source/2/page/x",
        "/v1/source/value/0",
        "/v1/source/value/abc",
    ],
)
async def test_malformed_ids_are_422(path):
    r = await get(build(), path)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_storage_trouble_is_503_not_a_leak():
    r = await get(build(storage=FakeStorage(error=NoCredentialsError())), "/v1/source/2/page/1")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"


async def test_without_the_planning_database_the_viewer_is_503():
    app = make_app(make_settings(), make_redis(), storage=FakeStorage())  # nodata, no repository
    r = await get(app, "/v1/source/2/page/1")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"


async def test_openapi_documents_both_routes():
    r = await get(build(), "/openapi.json")
    paths = r.json()["paths"]
    assert "/v1/source/{document_id}/page/{page}" in paths
    assert "/v1/source/value/{value_id}" in paths
    schema = r.json()["components"]["schemas"]["SourcePage"]
    assert set(schema["properties"]) >= {
        "document_id",
        "page",
        "url",
        "expires_at",
        "kind",
        "value",
    }
