"""Geometry review through the API (the pilot scope's ``staging.geometry_draft``, A2 check of
2026-09-29): the validity QA of a staged batch (invalid or empty geometry, the producing run's own
warnings; no topology QA in the POC), the queue with origin, QA and counts, the preview features,
approve and reject with their audit rows and the roles, and the publish job waiting for the
decisions and applying approved geometry only."""

from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import text

from core.geometry_qa import run_batch_qa
from tests.helpers import make_client
from tests.integration.test_publish_postgis import (  # noqa: F401 - fixtures
    ADMIN,
    EXPERT,
    REVIEWER,
    SQUARE,
    auth,
    publish,
    publish_env,
    reject_seeded_pending_item,
    reset_publish_state,
    rows,
    stage,
    storage,
    tiles,
)

pytestmark = pytest.mark.integration

LAT0, LNG0 = 42.4300, 19.2700
M_LAT = 1 / 111_132.0
M_LNG = 1 / (111_320.0 * math.cos(math.radians(LAT0)))
BOWTIE = "MULTIPOLYGON(((19.2 42.4, 19.201 42.401, 19.201 42.4, 19.2 42.401, 19.2 42.4)))"


@pytest.fixture(autouse=True)
async def _clean(postgis_url):
    await reset_publish_state(postgis_url)
    yield
    await reset_publish_state(postgis_url)


def box(x0: float, y0: float, x1: float, y1: float) -> str:
    """A rectangle in metres east / north of the test origin, as MULTIPOLYGON WKT (EPSG:4326)."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    ring = ", ".join(f"{LNG0 + x * M_LNG:.9f} {LAT0 + y * M_LAT:.9f}" for x, y in corners)
    return f"MULTIPOLYGON((({ring})))"


def parcel(number: str) -> dict:
    return {"document_id": 2, "urban_parcel_number": number}


PARCELS = [
    # Valid geometry that a topology QA would flag: UP 13 overlaps UP 12 by 1 m x 20 m, UP 91-94
    # leave a 2 m x 2 m sliver between them, and UP 12 is drawn at 400 m² against the plan's
    # stated 959.6 m². The POC checks validity only, so none of it is an issue.
    ("2|UP 12", box(0, 0, 20, 20), parcel("UP 12")),
    ("2|UP 13", box(19, 0, 39, 20), parcel("UP 13")),
    ("2|UP 91", box(100, 0, 110, 12), parcel("UP 91")),
    ("2|UP 92", box(112, 0, 122, 12), parcel("UP 92")),
    ("2|UP 93", box(110, 0, 112, 5), parcel("UP 93")),
    ("2|UP 94", box(110, 7, 112, 12), parcel("UP 94")),
]
ZONES_RUN = "podgorica-zones-qa-test"
# the zone import's own recorded warning (core.zones.validate Problem.to_json shape)
ZONES_VALIDATION = {
    "problems": [
        {
            "severity": "warning",
            "code": "document_adopted_without_reference",
            "message": "adopted but no eregistri_reference: confirm it against the registry",
            "zone_id": "centar",
            "zone_ids": ["centar"],
            "document": "row 1: DUP Centar",
            "area_m2": None,
        }
    ]
}


async def qa(app, batch_id: int):
    async with app.state.session_factory() as session:
        result = await run_batch_qa(session, batch_id, municipality_id="podgorica")
        await session.commit()
    return result


async def test_qa_flags_invalid_geometry_and_the_runs_own_warnings(publish_env):  # noqa: F811 - imported fixtures
    """A valid batch passes with no topology issues (overlaps, slivers and areas unlike the plan's
    are the reviewer's eye on the preview, not checks), the producing run's own warnings still reach
    its batch, and the preview marks nothing; invalid geometry fails in the test below."""
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        parcels = await stage(app, "urban_parcels", PARCELS)
        zones = await stage(app, "zones", [("centar", box(0, 0, 90, 90), {"name": "Centar"})])
        async with app.state.session_factory() as session:  # the run that staged the zones
            await session.execute(
                text(
                    "INSERT INTO zone_datasets (municipality_id, dataset_version, zones_batch_id, "
                    "validation) VALUES ('podgorica', :version, :batch, CAST(:report AS jsonb))"
                ),
                {"version": ZONES_RUN, "batch": zones, "report": json.dumps(ZONES_VALIDATION)},
            )
            await session.commit()
        try:
            passed = await qa(app, parcels)
            warned = await qa(app, zones)
            listing = await client.get("/v1/admin/geometry", headers=auth())
            detail = await client.get(f"/v1/admin/geometry/{parcels}", headers=auth())
            preview = await client.get(f"/v1/admin/geometry/{parcels}/features", headers=auth())
        finally:  # the publish reset does not know this row
            async with app.state.session_factory() as session:
                await session.execute(
                    text("DELETE FROM zone_datasets WHERE dataset_version = :version"),
                    {"version": ZONES_RUN},
                )
                await session.commit()

    assert passed.status == "pass" and passed.checks == ("validity",)
    assert passed.issues == []
    assert warned.status == "warn" and warned.checks == ("validity",)
    (issue,) = warned.issues
    assert issue.code == "zones.document_adopted_without_reference"
    assert issue.severity == "warning" and issue.count == 1 and issue.keys == ()
    assert issue.message == ZONES_VALIDATION["problems"][0]["message"]

    assert listing.status_code == 200 and detail.status_code == 200
    body = listing.json()
    assert body["counts"] == {"pending": 2, "approved": 0, "rejected": 0, "failing": 0}
    items = {i["id"]: i for i in body["items"]}
    assert set(items) == {parcels, zones}
    assert body["items"][0]["id"] == zones  # pending first, warnings before passes
    item = items[parcels]
    assert item == detail.json()
    assert item["layer_id"] == "urban_parcels" and item["layer_label"] == "Planned urban parcels"
    assert item["status"] == "staged" and item["review_status"] == "pending"
    assert item["qa_status"] == "pass" and item["qa_issues"] == [] and item["feature_count"] == 6
    assert item["can_approve"] and item["approve_blocker"] is None and item["can_reject"]
    west, south, east, north = item["bbox"]
    assert west == pytest.approx(LNG0, abs=1e-6) and north == pytest.approx(LAT0 + 20 * M_LAT)
    with_warning = items[zones]
    assert with_warning["qa_status"] == "warn" and with_warning["can_approve"]
    assert [i["code"] for i in with_warning["qa_issues"]] == [
        "zones.document_adopted_without_reference"
    ]
    assert with_warning["qa_issues"][0]["keys"] == []
    assert with_warning["dataset"] == {"kind": "zones", "version": ZONES_RUN, "status": "staged"}

    assert preview.status_code == 200
    features = preview.json()
    assert features["total"] == 6 and features["truncated"] is False
    marked = {f["id"]: f["properties"] for f in features["features"]["features"]}
    assert set(marked) == {key for key, _, _ in PARCELS}
    assert all(props["issues"] == [] for props in marked.values())
    assert marked["2|UP 12"]["label"] == "UP 12"
    assert marked["2|UP 12"]["area_m2"] == pytest.approx(400, abs=1)
    assert "gaps" not in features


async def test_decisions_are_audited_and_a_failing_batch_cannot_be_approved(publish_env):  # noqa: F811 - imported fixtures
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        broken = await stage(app, "land_use", [("lu-bad", BOWTIE, {"code": "S"})])
        good = await stage(app, "urban_blocks", [("2|A", box(0, 0, 50, 50), {"block_ref": "A"})])
        expert_list = await client.get("/v1/admin/geometry", headers=auth(EXPERT))
        expert_approve = await client.post(
            f"/v1/admin/geometry/{good}/approve", headers=auth(EXPERT)
        )
        nobody = await client.get("/v1/admin/geometry")
        # never checked (staged before 0033): QA runs on the first approval and fails
        refused = await client.post(f"/v1/admin/geometry/{broken}/approve", headers=auth())
        failing = (await client.get(f"/v1/admin/geometry/{broken}", headers=auth())).json()
        blank = await client.post(
            f"/v1/admin/geometry/{broken}/reject", json={"note": "   "}, headers=auth()
        )
        rejected = await client.post(
            f"/v1/admin/geometry/{broken}/reject",
            json={"note": "self-intersecting ring: redraw in QGIS"},
            headers=auth(),
        )
        again = await client.post(f"/v1/admin/geometry/{broken}/approve", headers=auth())
        approved = await client.post(
            f"/v1/admin/geometry/{good}/approve", json={"note": "  "}, headers=auth(ADMIN)
        )
        counts = (await client.get("/v1/admin/geometry", headers=auth())).json()["counts"]
        audit = await rows(
            app,
            "SELECT action, actor, entity_id, before, after, note, details FROM audit_log "
            "WHERE entity_type = 'geometry_batch' AND entity_id IN (:a, :b) ORDER BY id",
            a=broken,
            b=good,
        )

    assert expert_list.status_code == 403 and expert_approve.status_code == 403
    assert nobody.status_code == 401
    assert refused.status_code == 409
    assert refused.json()["error"]["details"] == {"batch_id": broken, "reason": "qa_failed"}
    assert failing["qa_status"] == "fail" and failing["approve_blocker"] == "qa_failed"
    assert failing["qa_issues"][0]["code"] == "invalid_geometry"
    assert failing["qa_issues"][0]["keys"] == ["lu-bad"]
    assert blank.status_code == 422
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "rejected" and body["review_status"] == "rejected"
    assert body["review_note"] == "self-intersecting ring: redraw in QGIS"
    assert body["reviewed_by"] == "vesna" and body["can_reject"] is False
    assert again.status_code == 409 and again.json()["error"]["details"]["reason"] == "rejected"
    assert approved.status_code == 200
    assert approved.json()["review_status"] == "approved" and approved.json()["qa_status"] == "pass"
    assert approved.json()["review_note"] is None  # a note of spaces is no note
    assert counts == {"pending": 0, "approved": 1, "rejected": 1, "failing": 0}

    assert [(a["action"], a["entity_id"]) for a in audit] == [
        ("geometry.reject", broken),
        ("geometry.approve", good),
    ]
    reject_row, approve_row = audit
    assert reject_row["actor"] == "vesna"
    assert reject_row["before"] == {
        "status": "staged",
        "review_status": "pending",
        "qa_status": "fail",
        "note": None,
    }
    assert reject_row["after"]["status"] == "rejected"
    assert reject_row["after"]["review_status"] == "rejected"
    assert reject_row["note"] == "self-intersecting ring: redraw in QGIS"
    assert reject_row["details"]["layer_id"] == "land_use"
    assert approve_row["actor"] == "ops"
    assert approve_row["before"]["review_status"] == "pending"
    assert approve_row["after"]["review_status"] == "approved"
    assert approve_row["after"]["qa_status"] == "pass"


async def test_publish_waits_for_the_review_and_applies_approved_geometry_only(publish_env, tiles):  # noqa: F811 - imported fixtures
    app = publish_env()
    test_parcel = {
        "ko_name": "Test KO",
        "parcel_number": "88",
        "sub_number": "",
        "street_address": "Nova 8",
    }
    async with app.router.lifespan_context(app), make_client(app) as client:
        await reject_seeded_pending_item(app)
        cadastral = await stage(app, "cadastral_parcels", [("Test KO|88|", SQUARE, test_parcel)])
        land_use = await stage(app, "land_use", [("lu-x", SQUARE, {"code": "S"})])
        refused = await client.post("/v1/admin/publish", json={}, headers=auth())
        status = (await client.get("/v1/admin/publish", headers=auth())).json()
        await client.post(f"/v1/admin/geometry/{cadastral}/approve", headers=auth())
        still = await client.post("/v1/admin/publish", json={}, headers=auth())
        await client.post(
            f"/v1/admin/geometry/{land_use}/reject",
            json={"note": "wrong plan's land use"},
            headers=auth(),
        )
        job = await publish(client, "geo-review-1")
        batches = await rows(
            app, "SELECT id, status, review_state FROM geometry_batches ORDER BY id"
        )
        served = await rows(
            app, "SELECT feature_key FROM layer_features WHERE feature_key = 'lu-x'"
        )
        parcels = await rows(
            app, "SELECT street_address FROM cadastral_parcels WHERE ko_name = 'Test KO'"
        )

    assert refused.status_code == 409
    details = refused.json()["error"]["details"]
    assert details["reason"] == "pending_review" and details["documents"] == []
    assert [g["batch_id"] for g in details["geometry"]] == [cadastral, land_use]
    assert details["geometry"][0]["layer_label"] == "Cadastral parcels"
    assert status["can_publish"] is False and len(status["geometry_blockers"]) == 2
    assert still.status_code == 409 and len(still.json()["error"]["details"]["geometry"]) == 1

    counts = job["result"]["counts"]
    assert counts["batches_published"] == 1 and counts["geometry"] == {"cadastral_parcels": 1}
    assert [(b["id"], b["status"], b["review_state"]) for b in batches] == [
        (cadastral, "published", "approved"),
        (land_use, "rejected", "rejected"),
    ]
    assert served == [] and parcels == [{"street_address": "Nova 8"}]
    assert "land_use" not in tiles.layers or all(
        f["properties"].get("feature_key") != "lu-x" for f in tiles.layers["land_use"]
    )


async def test_bulk_approval_of_a_dataset_skips_failing_batches(publish_env):  # noqa: F811 - imported fixtures
    app = publish_env()
    async with app.router.lifespan_context(app), make_client(app) as client:
        first = await stage(app, "urban_blocks", [("2|B", box(0, 0, 30, 30), {"block_ref": "B"})])
        second = await stage(app, "document_coverage", [("2", box(0, 0, 80, 80), {})])
        broken = await stage(app, "land_use", [("lu-bad", BOWTIE, {"code": "S"})])
        other = await stage(app, "zones", [("centar", box(0, 0, 90, 90), {"name": "Centar"})])
        async with app.state.session_factory() as session:  # test setup: one producing run
            await session.execute(
                text(
                    "UPDATE geometry_batches SET dataset_version = 'geo-2-test-1', "
                    "origin = 'vector_pdf', document_id = 2 WHERE id IN (:a, :b, :c)"
                ),
                {"a": first, "b": second, "c": broken},
            )
            await session.commit()
        bad = await client.post("/v1/admin/geometry/bulk-approve", json={}, headers=auth())
        result = await client.post(
            "/v1/admin/geometry/bulk-approve",
            json={"dataset_version": "geo-2-test-1", "note": "checked against the plan sheets"},
            headers=auth(),
        )
        by_document = await client.get(
            "/v1/admin/geometry", params={"document_id": 2}, headers=auth()
        )
        states = await rows(
            app, "SELECT id, review_state, review_note FROM geometry_batches ORDER BY id"
        )

    assert bad.status_code == 422
    assert result.status_code == 200
    assert result.json() == {
        "approved": [first, second],
        "skipped": [{"id": broken, "reason": "qa_failed"}],
    }
    listed = by_document.json()["items"]
    assert {d["id"] for d in listed} == {first, second, broken}
    assert listed[0]["id"] == broken  # pending first
    assert listed[0]["document"]["name"].startswith("DUP Centar")
    assert listed[0]["origin"] == "vector_pdf"
    assert {s["id"]: s["review_state"] for s in states} == {
        first: "approved",
        second: "approved",
        broken: "pending_review",
        other: "pending_review",
    }
