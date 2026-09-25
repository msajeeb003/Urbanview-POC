"""Expert review on PostGIS: the queue payload, approve / amend / reject transitions with their
audit rows (the AI value never overwritten), bulk approval, per-document counters and the publish
rule, the append-only audit_log (UPDATE / DELETE / TRUNCATE fail at the database), and the audit
listing. Storage is mocked; every other table is real."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.staff import create_user, issue_session
from tests.helpers import make_app, make_client, make_settings
from tests.integration.test_admin_pipeline_postgis import FakeStorage

pytestmark = pytest.mark.integration

TOKEN = "admin-token-1234"
RESTORE = (
    "DELETE FROM planning_parameter_extractions WHERE extracted_by = 'test'",
    "UPDATE planning_parameter_extractions SET review_state = 'pending_review', reviewer = NULL, "
    "reviewed_at = NULL, review_note = NULL, reviewed_by_user_id = NULL, "
    "amended_value_text = NULL, "
    "amended_value_number = NULL, amended_unit = NULL, published_value_id = NULL WHERE id = 1",
    "UPDATE planning_parameter_extractions SET review_state = 'rejected', reviewer = 'seed', "
    "reviewed_at = '2026-09-21T09:00:00Z', review_note = 'Misread: the table states 27.5 m.', "
    "reviewed_by_user_id = NULL, amended_value_text = NULL, amended_value_number = NULL, "
    "amended_unit = NULL, published_value_id = NULL WHERE id = 2",
    "DELETE FROM financial_assumptions WHERE created_by <> 'seed'",
    "UPDATE financial_assumptions SET is_current = true, retired_at = NULL, retired_by = NULL "
    "WHERE created_by = 'seed'",
    "DELETE FROM staff_sessions",
    "DELETE FROM staff_users WHERE municipality_id = 'podgorica'",
)


@pytest.fixture
def review_app(postgis_url):
    settings = make_settings(
        location_resolver="postgis",
        database_url=postgis_url,
        rate_limit_requests=100_000,
        admin_api_tokens=f"{TOKEN}:admin:ops",
    )
    return make_app(settings, storage=FakeStorage())


@pytest.fixture(autouse=True)
async def _restore_staging(postgis_url):
    """The seeded staging rows (a pending FAR and a rejected height for UP 12) are asserted by
    the panel tests; put them back after every test. audit_log is append-only, so nothing there."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    async def restore() -> None:
        engine = create_async_engine(postgis_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                for statement in RESTORE:
                    await session.execute(text(statement))
                await session.commit()
        finally:
            await engine.dispose()

    await restore()
    yield
    await restore()


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def insert_item(app, **cols) -> int:
    row = {
        "municipality_id": "podgorica",
        "document_id": 2,
        "urban_parcel_id": 1,
        "entity_type": "urban_parcel",
        "zone_id": None,
        "block_id": None,
        "field_key": "max_floors",
        "parameter_key": None,
        "value_text": None,
        "value_number": 6,
        "unit": None,
        "source_page": 20,
        "source_bbox": [72, 300, 520, 318],
        "source_note": "table 4",
        "raw_text": "spratnost P+5 (6 etaža)",
        "confidence": 0.82,
        "extracted_by": "test",
        "review_state": "pending_review",
    }
    row.update(cols)
    if row["parameter_key"] is None:
        row["parameter_key"] = row["field_key"]
    row["source_bbox"] = json.dumps(row["source_bbox"])
    async with app.state.session_factory() as session:
        item_id = (
            await session.execute(
                text(
                    "INSERT INTO planning_parameter_extractions (municipality_id, document_id, "
                    "urban_parcel_id, entity_type, zone_id, block_id, field_key, parameter_key, "
                    "value_text, value_number, unit, source_page, source_bbox, "
                    "source_note, raw_text, confidence, extracted_by, review_state) "
                    "VALUES (:municipality_id, :document_id, :urban_parcel_id, "
                    ":entity_type, :zone_id, :block_id, :field_key, :parameter_key, "
                    ":value_text, :value_number, :unit, :source_page, CAST(:source_bbox AS jsonb), "
                    ":source_note, :raw_text, :confidence, :extracted_by, "
                    "CAST(:review_state AS review_state)) RETURNING id"
                ),
                row,
            )
        ).scalar_one()
        await session.commit()
    return int(item_id)


async def staff_token(app, email: str, role: str) -> str:
    factory = app.state.session_factory
    await create_user(factory, municipality_id="podgorica", email=email, role=role)
    return await issue_session(
        factory, municipality_id="podgorica", email=email, created_via="test"
    )


# --- queue ----------------------------------------------------------------------------------------


async def test_queue_lists_everything_a_reviewer_needs(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        market_id = await insert_item(
            app,
            document_id=1,
            urban_parcel_id=None,
            entity_type="market_data",
            zone_id=1,
            field_key=None,
            parameter_key="sale_rate_eur_m2",
            value_number=2400,
            unit="EUR/m²",
            source_page=3,
            raw_text="prosječna cijena 2.400 €/m²",
        )
        reviewer = await staff_token(app, "reviewer@example.com", "reviewer")
        expert = await staff_token(app, "expert@example.com", "expert")
        pending = await client.get("/v1/admin/review", params={"status": "pending"}, headers=auth())
        one = await client.get("/v1/admin/review", params={"limit": 1}, headers=auth())
        rejected = await client.get(
            "/v1/admin/review", params={"status": "rejected"}, headers=auth()
        )
        none = await client.get("/v1/admin/review", params={"document_id": 4}, headers=auth())
        zone = await client.get("/v1/admin/review", params={"zone_id": 1}, headers=auth())
        market = await client.get(
            "/v1/admin/review", params={"entity_type": "market_data"}, headers=auth()
        )
        page13 = await client.get(
            "/v1/admin/review", params={"document_id": 2, "source_page": 13}, headers=auth()
        )
        as_reviewer = await client.get("/v1/admin/review", headers=auth(reviewer))
        as_expert = await client.get("/v1/admin/review", headers=auth(expert))
        anonymous = await client.get("/v1/admin/review")
        single = await client.get("/v1/admin/review/1", headers=auth())

    assert pending.status_code == 200, pending.text
    items = pending.json()["items"]
    item = next(i for i in items if i["id"] == 1)
    assert item["status"] == "pending" and item["parameter_key"] == "max_far"
    assert item["label_en"] and item["label_me"] and item["value_type"] == "number"
    assert item["extracted"]["number"] == 9.9 and item["extracted"]["text"] is None
    assert item["amended"] is None and item["effective"] == item["extracted"]
    target = item["target"]
    assert target["entity_type"] == "urban_parcel" and target["urban_parcel_id"] == 1
    assert target["urban_parcel_number"] and target["zone_name"] == "Centar"
    source = item["source"]
    assert source["document_id"] == 2 and source["document_name"] == "DUP Centar – Zona C2"
    assert source["page"] == 13 and source["bbox"] == [72, 388, 520, 406]
    assert source["bbox_space"] == "pdf-points-bottom-left" and "staging" in source["note"]
    assert source["registry_url"]
    assert source["link"]["kind"] == "pdf_page"
    assert source["link"]["url"].endswith("/2/document.pdf?X-Amz-Signature=sig#page=13")
    assert source["link"]["expires_at"]
    assert item["extracted_by"] == "llm:sample" and item["published"] is False
    assert item["reviewed_by"] is None

    market_item = next(i for i in items if i["id"] == market_id)
    assert market_item["target"] == {
        "entity_type": "market_data",
        "urban_parcel_id": None,
        "urban_parcel_number": None,
        "block_id": None,
        "block_ref": None,
        "zone_id": 1,
        "zone_name": "Centar",
    }
    assert market_item["label_en"] == "Selling price per m²"
    assert market_item["extracted"] == {"text": None, "number": 2400.0, "unit": "EUR/m²"}
    assert (
        market_item["source"]["raw_text"].startswith("prosječna")
        and market_item["source"]["confidence"] == 0.82
    )

    assert one.json()["total"] >= 2 and len(one.json()["items"]) == 1
    assert one.json()["items"][0]["status"] == "pending"  # pending first
    assert [i["id"] for i in rejected.json()["items"]] == [2]
    assert none.json()["total"] == 0 and none.json()["items"] == []
    assert 1 in [i["id"] for i in zone.json()["items"]]
    assert [i["id"] for i in market.json()["items"]] == [market_id]
    assert [i["id"] for i in page13.json()["items"]] == [1]
    assert as_reviewer.status_code == 200 and as_expert.status_code == 200
    assert anonymous.status_code == 401
    assert single.status_code == 200 and single.json()["id"] == 1


# --- decisions ------------------------------------------------------------------------------------


async def test_approve_amend_reject_keep_the_ai_value_and_write_audit_rows(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reviewer = await staff_token(app, "reviewer@example.com", "reviewer")
        text_id = await insert_item(
            app, field_key="land_use", value_number=None, value_text="Stanovanje", source_page=12
        )
        approved = await client.post("/v1/admin/review/1/approve", headers=auth(reviewer))
        amended = await client.post(
            "/v1/admin/review/1/amend",
            json={"value": 3.4, "note": "table 3 says 3.4"},
            headers=auth(reviewer),
        )
        wrong_type = await client.post(
            "/v1/admin/review/1/amend", json={"value": "three point four"}, headers=auth(reviewer)
        )
        text_amended = await client.post(
            f"/v1/admin/review/{text_id}/amend",
            json={"value": "Stanovanje – mješovita namjena", "note": "full wording"},
            headers=auth(reviewer),
        )
        text_wrong = await client.post(
            f"/v1/admin/review/{text_id}/amend", json={"value": 12}, headers=auth(reviewer)
        )
        rejected = await client.post(
            "/v1/admin/review/1/reject", json={"note": "wrong table"}, headers=auth(reviewer)
        )
        no_reason = await client.post("/v1/admin/review/1/reject", json={}, headers=auth(reviewer))
        re_approved = await client.post(
            "/v1/admin/review/1/approve", json={"note": "second look"}, headers=auth()
        )
        missing = await client.post("/v1/admin/review/999999/approve", headers=auth())
        async with app.state.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE planning_parameter_extractions SET published_value_id = 1 "
                    "WHERE id = :id"
                ),
                {"id": text_id},
            )
            await session.commit()
        closed = await client.post(f"/v1/admin/review/{text_id}/approve", headers=auth())
        trail = await client.get(
            "/v1/admin/audit",
            params={"entity_type": "extraction_item", "entity_id": 1},
            headers=auth(),
        )

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewed_by"] == "reviewer@example.com"
    assert approved.json()["reviewed_at"].endswith("Z")

    assert amended.status_code == 200, amended.text
    body = amended.json()
    assert body["status"] == "amended"
    assert body["extracted"]["number"] == 9.9  # the AI value stays retrievable
    assert body["amended"]["number"] == 3.4 and body["effective"]["number"] == 3.4
    assert body["review_note"] == "table 3 says 3.4"
    assert wrong_type.status_code == 422 and "number" in wrong_type.text

    assert text_amended.status_code == 200, text_amended.text
    assert text_amended.json()["extracted"]["text"] == "Stanovanje"
    assert text_amended.json()["effective"]["text"] == "Stanovanje – mješovita namjena"
    assert text_wrong.status_code == 422 and "text" in text_wrong.text

    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert rejected.json()["amended"] is None  # a rejection clears the correction
    assert rejected.json()["effective"]["number"] == 9.9
    assert no_reason.status_code == 422
    assert re_approved.status_code == 200 and re_approved.json()["status"] == "approved"
    assert missing.status_code == 404
    assert closed.status_code == 409
    assert closed.json()["error"]["details"]["reason"] == "published"

    assert trail.status_code == 200
    entries = trail.json()["items"]  # newest first
    assert [e["action"] for e in entries] == [
        "review.approve",
        "review.reject",
        "review.amend",
        "review.approve",
    ]
    amend_entry = entries[2]
    assert amend_entry["actor"] == "reviewer@example.com" and amend_entry["actor_user_id"]
    assert amend_entry["before"]["status"] == "approved"
    assert amend_entry["before"]["value"]["number"] == 9.9
    assert amend_entry["after"]["status"] == "amended"
    assert amend_entry["after"]["value"]["number"] == 3.4
    assert amend_entry["note"] == "table 3 says 3.4"
    assert amend_entry["details"]["parameter_key"] == "max_far"
    assert entries[0]["actor"] == "ops" and entries[0]["note"] == "second look"


async def test_bulk_approve_by_page_and_by_ids(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        a = await insert_item(app, source_page=20)
        b = await insert_item(
            app, field_key="max_site_coverage_pct", value_number=60, source_page=20
        )
        c = await insert_item(app, field_key="max_height_m", value_number=18, source_page=21)
        e = await insert_item(
            app, field_key="max_far", value_number=2, source_page=20, review_state="rejected"
        )
        by_page = await client.post(
            "/v1/admin/review/bulk-approve",
            json={"document_id": 2, "source_page": 20, "note": "page 20 checked"},
            headers=auth(),
        )
        by_ids = await client.post(
            "/v1/admin/review/bulk-approve",
            json={"item_ids": [c, e, a, 999999]},
            headers=auth(),
        )
        by_parcel = await client.post(
            "/v1/admin/review/bulk-approve", json={"urban_parcel_id": 1}, headers=auth()
        )
        trail = await client.get(
            "/v1/admin/audit", params={"action": "review.approve", "limit": 10}, headers=auth()
        )
    assert by_page.status_code == 200, by_page.text
    assert by_page.json() == {"approved": [a, b], "skipped": []}  # e is rejected, not pending
    assert by_ids.status_code == 200
    assert by_ids.json()["approved"] == [c]
    assert by_ids.json()["skipped"] == [
        {"id": e, "reason": "not_pending"},
        {"id": a, "reason": "not_pending"},
        {"id": 999999, "reason": "not_found"},
    ]
    assert by_parcel.json()["approved"] == [1]  # the seeded pending FAR of UP 12
    approved_ids = [e["entity_id"] for e in trail.json()["items"] if e["note"] == "page 20 checked"]
    assert sorted(approved_ids) == [a, b]  # one audit row per item


# --- counters -------------------------------------------------------------------------------------


async def test_document_counters_and_the_publish_rule(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await insert_item(app, source_page=20)
        before = await client.get(
            "/v1/admin/review/summary", params={"document_id": 2}, headers=auth()
        )
        document_before = await client.get("/v1/admin/documents/2", headers=auth())
        await client.post("/v1/admin/review/bulk-approve", json={"document_id": 2}, headers=auth())
        after = await client.get(
            "/v1/admin/review/summary", params={"document_id": 2}, headers=auth()
        )
        document_after = await client.get("/v1/admin/documents/2", headers=auth())
        everything = await client.get("/v1/admin/review/summary", headers=auth())
    assert before.status_code == 200
    (counters,) = before.json()
    assert counters["document_name"] == "DUP Centar – Zona C2"
    assert counters["pending"] == 2 and counters["rejected"] == 1 and counters["total"] == 3
    assert counters["can_publish"] is False
    assert counters["publish_blockers"] == [
        "2 item(s) pending review",
        "nothing approved or amended",
    ]
    assert document_before.json()["review"]["can_publish"] is False
    (counters,) = after.json()
    assert counters["pending"] == 0 and counters["approved"] == 2
    assert counters["can_publish"] is True and counters["publish_blockers"] == []
    assert document_after.json()["review"] == {
        "pending": 0,
        "approved": 2,
        "amended": 0,
        "rejected": 1,
        "total": 3,
        "can_publish": True,
        "publish_blockers": [],
    }
    assert [c["document_id"] for c in everything.json()] == [2]  # only documents with items


# --- audit log ------------------------------------------------------------------------------------


async def test_audit_rows_cannot_be_modified_or_removed(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        await client.post("/v1/admin/review/1/approve", json={"note": "keep me"}, headers=auth())
        factory = app.state.session_factory
        async with factory() as session:
            audit_id = (
                await session.execute(
                    text("SELECT max(id) FROM audit_log WHERE entity_type = 'extraction_item'")
                )
            ).scalar_one()
        for statement in (
            "UPDATE audit_log SET note = 'tampered' WHERE id = :id",
            "DELETE FROM audit_log WHERE id = :id",
            "TRUNCATE audit_log",
        ):
            async with factory() as session:
                with pytest.raises(DBAPIError) as refused:
                    await session.execute(text(statement), {"id": audit_id})
                assert "append-only" in str(refused.value)
        async with factory() as session:
            note = (
                await session.execute(
                    text("SELECT note FROM audit_log WHERE id = :id"), {"id": audit_id}
                )
            ).scalar_one()
    assert note == "keep me"


async def test_audit_listing_filters_who_changed_what_and_when(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reviewer = await staff_token(app, "reviewer@example.com", "reviewer")
        expert = await staff_token(app, "expert@example.com", "expert")
        started = datetime.now(UTC)
        await client.post("/v1/admin/review/1/approve", headers=auth(reviewer))
        created = await client.post(
            "/v1/admin/assumptions",
            json={
                "zone_id": 1,
                "land_rate": {"expected": 1400},
                "build_rate": {"expected": 900},
                "design_rate": {"expected": 95},
                "sale_rate": {"expected": 2500},
                "source": "Realitica (audit test)",
            },
            headers=auth(),
        )
        by_actor = await client.get(
            "/v1/admin/audit", params={"actor": "reviewer@example.com"}, headers=auth()
        )
        by_entity = await client.get(
            "/v1/admin/audit",
            params={"entity_type": "financial_assumptions", "entity_id": created.json()["id"]},
            headers=auth(),
        )
        since = await client.get(
            "/v1/admin/audit", params={"from": started.isoformat(), "limit": 100}, headers=auth()
        )
        paged = await client.get(
            "/v1/admin/audit", params={"limit": 1, "offset": 1}, headers=auth()
        )
        as_reviewer = await client.get("/v1/admin/audit", headers=auth(reviewer))
        as_expert = await client.get("/v1/admin/audit", headers=auth(expert))
    assert by_actor.status_code == 200
    actors = {e["actor"] for e in by_actor.json()["items"]}
    assert actors == {"reviewer@example.com"}
    assert by_actor.json()["items"][0]["action"] == "review.approve"
    (entry,) = by_entity.json()["items"]
    assert entry["action"] == "assumptions.create" and entry["actor"] == "ops"
    assert entry["before"]["version"] == 1 and entry["before"]["land_rate_eur_m2"] == 1350
    assert entry["after"]["version"] == 2 and entry["after"]["land_rate"]["expected"] == 1400
    actions = [e["action"] for e in since.json()["items"]]
    assert actions[:2] == ["assumptions.create", "review.approve"]  # newest first
    assert since.json()["total"] >= 2  # users made through core.staff write no audit rows
    assert (
        paged.json()["limit"] == 1
        and paged.json()["offset"] == 1
        and len(paged.json()["items"]) == 1
    )
    assert as_reviewer.status_code == 200
    assert as_expert.status_code == 403
