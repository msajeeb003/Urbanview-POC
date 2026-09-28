"""Files of a document version (migration 0022) through the staff API: registration with several
files and roles, adding files (idempotent), per-file extraction, the document state and the list
filters, removing a file (its open items superseded; refused once an item was approved), the
primary file, the audit trail, and the pipeline routes open to reviewers but not experts."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from core.seeds import placeholder_pdf
from tests.helpers import make_app, make_client, make_settings
from tests.integration import test_admin_pipeline_postgis as pipeline

# The pipeline tests' fixtures (fake storage and dispatcher, the app, the clean-up around each
# test) and helpers.
storage = pipeline.storage
dispatcher = pipeline.dispatcher
admin_app = pipeline.admin_app
_clean_admin_rows = pipeline._clean_admin_rows
PDF_A, PDF_B, ZIP = pipeline.PDF_A, pipeline.PDF_B, pipeline.ZIP
auth, upload, audit_actions = pipeline.auth, pipeline.upload, pipeline.audit_actions

pytestmark = pytest.mark.integration

PDF_C = placeholder_pdf("DUP Test C annex", 2)


async def sql(app, statement: str, **params):
    async with app.state.session_factory() as session:
        result = await session.execute(text(statement), params)
        await session.commit()
        return result


async def finish_run(app, job_id: int, *, items: list[tuple[str, str]]) -> None:
    """What the worker would leave behind: the run ready for review with document-level items
    (``(field_key, review_state)``) and its job succeeded."""
    run = (
        await sql(app, "SELECT id, document_id FROM extraction_runs WHERE job_id = :j", j=job_id)
    ).one()
    for field_key, state in items:
        await sql(
            app,
            "INSERT INTO planning_parameter_extractions (municipality_id, document_id, "
            "entity_type, field_key, parameter_key, value_number, source_page, extracted_by, "
            "review_state, run_id) VALUES ('podgorica', :d, 'document', :f, :f, 2.5, 1, 'test', "
            "CAST(:s AS review_state), :r)",
            d=run[1],
            f=field_key,
            s=state,
            r=run[0],
        )
    await sql(
        app,
        "UPDATE extraction_runs SET status = 'ready_for_review', finished_at = now() WHERE id = :r",
        r=run[0],
    )
    await sql(
        app,
        "UPDATE pipeline_jobs SET status = 'succeeded', finished_at = now() WHERE id = :j",
        j=job_id,
    )


def by_file(document: dict) -> dict[int, dict]:
    return {f["file_id"]: f for f in document["files"]}


async def test_a_version_with_several_files(admin_app):
    app = admin_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        text_pdf = (await upload(client, PDF_A, "text.pdf")).json()["file"]
        sheet_pdf = (await upload(client, PDF_B, "sheet.pdf")).json()["file"]
        annex_pdf = (await upload(client, PDF_C, "annex.pdf")).json()["file"]
        gis = (await upload(client, ZIP, "layers.zip", kind="gis", mime="application/zip")).json()[
            "file"
        ]
        created = await client.post(
            "/v1/admin/documents",
            json={
                "name": "DUP Files",
                "type": "DUP",
                "status": "adopted",
                "zone_id": 1,
                "files": [
                    {"file_id": sheet_pdf["id"], "role": "drawing"},
                    {"file_id": text_pdf["id"], "role": "text"},
                ],
            },
            headers=auth(),
        )
        assert created.status_code == 201, created.text
        doc = created.json()
        doc_id = doc["id"]
        gis_as_text = await client.post(
            f"/v1/admin/documents/{doc_id}/files",
            json={"files": [{"file_id": gis["id"], "role": "text"}]},
            headers=auth(),
        )
        added = await client.post(
            f"/v1/admin/documents/{doc_id}/files",
            json={
                "files": [
                    {"file_id": gis["id"], "role": "drawing"},
                    {"file_id": annex_pdf["id"], "role": "text"},
                ]
            },
            headers=auth(),
        )
        again = await client.post(
            f"/v1/admin/documents/{doc_id}/files",
            json={"files": [{"file_id": annex_pdf["id"], "role": "both"}]},
            headers=auth(),
        )
        default_extract = await client.post(
            f"/v1/admin/documents/{doc_id}/jobs/extract", headers=auth()
        )
        annex_extract = await client.post(
            f"/v1/admin/documents/{doc_id}/jobs/extract",
            params={"file_id": annex_pdf["id"]},
            headers=auth(),
        )
        drawing_extract = await client.post(
            f"/v1/admin/documents/{doc_id}/jobs/extract",
            params={"file_id": sheet_pdf["id"]},
            headers=auth(),
        )
        stranger_extract = await client.post(
            f"/v1/admin/documents/{doc_id}/jobs/extract",
            params={"file_id": 999_999},
            headers=auth(),
        )
        processing = (await client.get(f"/v1/admin/documents/{doc_id}", headers=auth())).json()
        by_state = await client.get(
            "/v1/admin/documents", params={"state": "processing"}, headers=auth()
        )
        by_job = await client.get(
            "/v1/admin/documents", params={"job_state": "queued", "zone_id": 1}, headers=auth()
        )
        by_name = await client.get("/v1/admin/documents", params={"q": "dup fil"}, headers=auth())
        busy = await client.delete(
            f"/v1/admin/documents/{doc_id}/files/{text_pdf['id']}", headers=auth()
        )

        text_job = default_extract.json()
        annex_job = annex_extract.json()
        await finish_run(app, text_job["id"], items=[("max_far", "pending_review")])
        await finish_run(app, annex_job["id"], items=[("max_height_m", "approved")])
        ready = (await client.get(f"/v1/admin/documents/{doc_id}", headers=auth())).json()
        queue = await client.get(
            "/v1/admin/review",
            params={"document_id": doc_id, "file_id": text_pdf["id"]},
            headers=auth(),
        )
        approved_file = await client.delete(
            f"/v1/admin/documents/{doc_id}/files/{annex_pdf['id']}", headers=auth()
        )
        removed = await client.delete(
            f"/v1/admin/documents/{doc_id}/files/{text_pdf['id']}", headers=auth()
        )
        role = await client.patch(
            f"/v1/admin/documents/{doc_id}/files/{sheet_pdf['id']}",
            json={"role": "both"},
            headers=auth(),
        )
        gis_role = await client.patch(
            f"/v1/admin/documents/{doc_id}/files/{gis['id']}",
            json={"role": "both"},
            headers=auth(),
        )
        file_record = (
            await client.get(f"/v1/admin/files/{annex_pdf['id']}", headers=auth())
        ).json()
        superseded = (
            await sql(
                app,
                "SELECT count(*) FROM planning_parameter_extractions "
                "WHERE document_id = :d AND field_key = 'max_far' AND superseded_at IS NOT NULL",
                d=doc_id,
            )
        ).scalar_one()

    # registration: two files in the order given; the primary is the first text / both PDF
    assert [f["file_id"] for f in doc["files"]] == [sheet_pdf["id"], text_pdf["id"]]
    assert doc["file"]["id"] == text_pdf["id"] and doc["page_count"] == 3
    assert by_file(doc)[text_pdf["id"]]["is_primary"] is True
    assert doc["state"] == "not_extracted" and doc["versions"][0]["file_count"] == 2

    # adding: GIS files only as drawings; a file already on the version is left alone
    assert gis_as_text.status_code == 422
    assert added.status_code == 201, added.text
    assert [f["file_id"] for f in added.json()["files"]][-2:] == [gis["id"], annex_pdf["id"]]
    assert again.status_code == 200 and by_file(again.json())[annex_pdf["id"]]["role"] == "text"

    # extraction per file: the default is the primary text file; drawings and strangers refused
    assert default_extract.status_code == 202, default_extract.text
    assert text_job["file_id"] == text_pdf["id"]
    assert text_job["payload"] == {"document_id": doc_id, "file_id": text_pdf["id"]}
    assert annex_extract.status_code == 202 and annex_job["file_id"] == annex_pdf["id"]
    assert drawing_extract.status_code == 409
    assert drawing_extract.json()["error"]["details"]["reason"] == "drawing_file"
    assert stranger_extract.status_code == 404

    # state and filters while the runs are queued
    files = by_file(processing)
    assert processing["state"] == "processing"
    assert files[text_pdf["id"]]["extraction_state"] == "queued"
    assert files[text_pdf["id"]]["extraction_job"]["id"] == text_job["id"]
    assert files[sheet_pdf["id"]]["extraction_state"] == "none"
    assert files[text_pdf["id"]]["can_remove"] is False
    assert files[text_pdf["id"]]["remove_blocker"] == "extraction_active"
    assert [d["id"] for d in by_state.json()["items"]] == [doc_id]
    assert by_state.json()["total"] == 1
    assert [d["id"] for d in by_job.json()["items"]] == [doc_id]
    assert [d["id"] for d in by_name.json()["items"]] == [doc_id]
    assert busy.status_code == 409
    assert busy.json()["error"]["details"]["reason"] == "extraction_active"

    # runs finished: items counted per file, the state follows the pending item
    files = by_file(ready)
    assert ready["state"] == "ready_for_review"
    assert files[text_pdf["id"]]["extraction_state"] == "ready_for_review"
    assert files[text_pdf["id"]]["items"]["pending"] == 1
    assert files[annex_pdf["id"]]["items"]["approved"] == 1
    assert files[annex_pdf["id"]]["remove_blocker"] == "items_accepted"
    assert files[text_pdf["id"]]["can_remove"] is True
    assert queue.status_code == 200 and queue.json()["total"] == 1
    assert queue.json()["items"][0]["source"]["file_id"] == text_pdf["id"]
    assert queue.json()["items"][0]["source"]["file_name"] == "text.pdf"

    # removing: refused once an item was approved; otherwise its open items are superseded
    assert approved_file.status_code == 409
    assert approved_file.json()["error"]["details"]["reason"] == "items_accepted"
    assert removed.status_code == 200, removed.text
    after = removed.json()
    assert text_pdf["id"] not in by_file(after) and superseded == 1
    assert after["file"]["id"] == annex_pdf["id"]  # the next text PDF became the primary
    assert after["state"] == "reviewed"  # nothing pending, the approved item is not published

    # roles: PDFs any role, GIS files drawings only
    assert role.status_code == 200 and by_file(role.json())[sheet_pdf["id"]]["role"] == "both"
    assert gis_role.status_code == 422
    assert file_record["document_ids"] == [doc_id]

    actions = [a["action"] for a in await audit_actions(app, "planning_document", doc_id)]
    for action in ("document.register", "document.file_attach", "document.file_detach"):
        assert action in actions
    assert "document.file_role" in actions


async def test_reviewers_read_documents_admins_change_them(postgis_url, storage, dispatcher):
    """The pilot scope's roles: admins run the pipeline, reviewers read documents, files and jobs
    (read-only documents), experts have no pipeline access."""
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens="admin-token-1234:admin:ops,reviewer-tok-1234:reviewer:rev,"
        "expert-token-12345:expert:exp",
    )
    app = make_app(settings, storage=storage, admin_dispatcher=dispatcher)
    async with app.router.lifespan_context(app), make_client(app) as client:
        refused_upload = await upload(client, PDF_A, "reviewer.pdf", token="reviewer-tok-1234")
        uploaded = await upload(client, PDF_A, "admin.pdf", token="admin-token-1234")
        file_id = uploaded.json()["file"]["id"]
        body = {
            "name": "DUP Reviewer",
            "type": "PUP",
            "status": "in_progress",
            "files": [{"file_id": file_id, "role": "both"}],
        }
        refused_register = await client.post(
            "/v1/admin/documents", json=body, headers=auth("reviewer-tok-1234")
        )
        registered = await client.post(
            "/v1/admin/documents", json=body, headers=auth("admin-token-1234")
        )
        document_id = registered.json()["id"]
        refused_live = await client.patch(
            f"/v1/admin/documents/{document_id}/coverage",
            json={"live": True},
            headers=auth("reviewer-tok-1234"),
        )
        refused_extract = await client.post(
            f"/v1/admin/documents/{document_id}/jobs/extract",
            headers=auth("reviewer-tok-1234"),
        )
        documents = await client.get("/v1/admin/documents", headers=auth("reviewer-tok-1234"))
        document = await client.get(
            f"/v1/admin/documents/{document_id}", headers=auth("reviewer-tok-1234")
        )
        files = await client.get("/v1/admin/files", headers=auth("reviewer-tok-1234"))
        jobs = await client.get("/v1/admin/jobs", headers=auth("reviewer-tok-1234"))
        expert = await client.get("/v1/admin/documents", headers=auth("expert-token-12345"))
    assert refused_upload.status_code == 403, refused_upload.text
    assert refused_upload.json()["error"]["details"]["required_roles"] == ["admin"]
    for refused in (refused_register, refused_live, refused_extract):
        assert refused.status_code == 403, refused.text
    assert uploaded.status_code == 201, uploaded.text
    assert registered.status_code == 201, registered.text
    assert registered.json()["registered_by"] == "ops"
    assert documents.status_code == 200 and document.status_code == 200
    assert files.status_code == 200 and jobs.status_code == 200
    assert expert.status_code == 403
