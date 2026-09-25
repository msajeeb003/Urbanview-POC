"""Admin pipeline pieces that need no database: upload validation, filename sanitising, the
Celery dispatcher (apply_async mocked), token helpers, and the role gate with staff sessions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from api.services.admin import safe_filename, validate_upload
from core.auth import Principal, Role, bearer_token, generate_token, hash_token
from core.errors import AppError
from jobs.enqueue import CeleryDispatcher, import_tasks
from tests.helpers import make_app, make_client, make_redis, make_settings

PDF = b"%PDF-1.4\n1 0 obj\n"
ZIP = b"PK\x03\x04rest-of-archive"
GPKG = b"SQLite format 3\x00rest"


@pytest.mark.parametrize(
    ("kind", "filename", "content_type", "head", "mime"),
    [
        ("planning_document", "DUP Centar.pdf", "application/pdf", PDF, "application/pdf"),
        ("planning_document", "dup.PDF", "application/octet-stream", PDF, "application/pdf"),
        ("planning_document", "dup.pdf", None, PDF, "application/pdf"),
        ("gis", "parcels.zip", "application/zip", ZIP, "application/zip"),
        ("gis", "parcels.geojson", "", b'{"type":"Feature"}', "application/geo+json"),
        ("gis", "plan.gpkg", None, GPKG, "application/geopackage+sqlite3"),
        ("gis", "plan.pdf", "application/pdf; charset=binary", PDF, "application/pdf"),
        ("cadastral_extract", "extract.csv", "text/csv", b"ko;number\n", "text/csv"),
        (
            "cadastral_extract",
            "extract.xlsx",
            None,
            ZIP,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_accepted_uploads(kind, filename, content_type, head, mime):
    extension, recorded = validate_upload(kind, filename, content_type, head)
    assert extension == "." + filename.rsplit(".", 1)[1].lower()
    assert recorded == mime


@pytest.mark.parametrize(
    ("kind", "filename", "content_type", "head", "hint"),
    [
        ("planning_document", "plan.zip", "application/zip", ZIP, "not accepted for kind"),
        ("planning_document", "plan.pdf", "application/pdf", b"<html>", "does not look like"),
        ("planning_document", "plan.pdf", "text/html", PDF, "content type"),
        ("gis", "notes.docx", None, ZIP, "not accepted for kind"),
        ("cadastral_extract", "extract.exe", None, b"MZ", "not accepted for kind"),
        ("planning_document", "noextension", None, PDF, "without extension"),
        ("gis", "layers.zip", None, b"not a zip", "does not look like"),
    ],
)
def test_rejected_uploads(kind, filename, content_type, head, hint):
    with pytest.raises(AppError) as info:
        validate_upload(kind, filename, content_type, head)
    assert info.value.status_code == 422
    assert info.value.code == "validation_error"
    assert any(hint in problem["msg"] for problem in info.value.details)


def test_safe_filename():
    assert safe_filename("../../DUP Centar – Zona C2.pdf") == "DUP_Centar_Zona_C2.pdf"
    assert safe_filename("C:\\plans\\plan v2 (final).PDF") == "plan_v2_final.PDF"
    assert (
        safe_filename("Izmjene i dopune DUP-a Čuvari 2023.pdf")
        == "Izmjene_i_dopune_DUP-a_Cuvari_2023.pdf"
    )
    assert safe_filename("", fallback="upload.pdf") == "upload.pdf"
    assert safe_filename("...") == "file"
    long = safe_filename("x" * 300 + ".geojson")
    assert len(long) <= 120 and long.endswith(".geojson")


def test_celery_dispatcher_applies_the_registered_task_on_its_queue():
    from jobs.celery_app import celery_app

    import_tasks()
    extract = celery_app.tasks["jobs.tasks.extraction.extract_document"]
    geo = celery_app.tasks["jobs.tasks.ingestion.process_geometry"]
    with mock.patch.object(
        extract, "apply_async", return_value=SimpleNamespace(id="task-123")
    ) as send:
        assert CeleryDispatcher().enqueue("extract_document", 42, "podgorica") == "task-123"
        send.assert_called_once_with(args=[42, "podgorica"], queue="extraction")
    with mock.patch.object(geo, "apply_async", return_value=SimpleNamespace(id="task-9")) as send:
        assert CeleryDispatcher().enqueue("process_geometry", 7, "podgorica") == "task-9"
        send.assert_called_once_with(args=[7, "podgorica"], queue="geo")
    with pytest.raises(ValueError):
        CeleryDispatcher().enqueue("publish", 1, "podgorica")


def test_token_helpers():
    token = generate_token()
    assert len(token) >= 40 and token != generate_token()
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert hash_token("abc") == hash_token("abc") and len(hash_token("abc")) == 64
    assert hash_token("abc") != hash_token("abd")
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer   abc ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("Bearer") is None and bearer_token(None) is None


class FakeStaffAuthenticator:
    def __init__(self, sessions: dict[str, Principal]) -> None:
        self.sessions = sessions

    async def authenticate(self, authorization):
        return self.sessions.get(bearer_token(authorization) or "")


async def test_staff_sessions_pass_the_role_gate():
    staff = FakeStaffAuthenticator(
        {
            "sess-admin-0001": Principal("ana@example.com", Role.admin, user_id=7),
            "sess-review-001": Principal("bob@example.com", Role.reviewer, user_id=8),
        }
    )
    app = make_app(make_settings(rate_limit_requests=100), make_redis(), staff_authenticator=staff)
    async with app.router.lifespan_context(app), make_client(app) as client:
        admin = await client.get(
            "/v1/admin/files", headers={"Authorization": "Bearer sess-admin-0001"}
        )
        reviewer = await client.get(
            "/v1/admin/files", headers={"Authorization": "Bearer sess-review-001"}
        )
        unknown = await client.get("/v1/admin/files", headers={"Authorization": "Bearer nope"})
        anonymous = await client.get("/v1/admin/files")
    # the gate passed for the admin session; the staff API itself needs PostGIS (503 in nodata)
    assert admin.status_code == 503
    assert admin.json()["error"]["code"] == "service_unavailable"
    assert reviewer.status_code == 403
    assert unknown.status_code == 401 and anonymous.status_code == 401


async def test_openapi_documents_the_staff_routes():
    app = make_app(make_settings(), make_redis())
    async with app.router.lifespan_context(app), make_client(app) as client:
        paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/v1/admin/files" in paths and "post" in paths["/v1/admin/files"]
    assert "/v1/admin/documents" in paths
    assert "/v1/admin/documents/{document_id}/jobs/extract" in paths
    assert "/v1/admin/files/{file_id}/jobs/geo" in paths
    assert "/v1/admin/documents/{document_id}/coverage" in paths
    assert "/v1/admin/jobs/{job_id}" in paths


def test_document_adoption_date_is_optional_and_never_in_the_future():
    from datetime import date, timedelta

    from pydantic import ValidationError

    from api.schemas.admin import DocumentIn

    base = {"file_id": 1, "name": "DUP Centar", "type": "DUP", "status": "adopted"}
    assert DocumentIn(**base).adopted_on is None
    assert DocumentIn(**base, adopted_on="2019-05-12").adopted_on == date(2019, 5, 12)
    with pytest.raises(ValidationError, match="future"):
        DocumentIn(**base, adopted_on=date.today() + timedelta(days=1))
