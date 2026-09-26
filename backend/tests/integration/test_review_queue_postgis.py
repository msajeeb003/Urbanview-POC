"""The review queue as the admin console reads it: sort orders (pending first / page then parcel /
lowest confidence first), the run block (job and cost) and the field's unit on every item, and the
wordings a document already uses for a text field (the amend select)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.helpers import make_client
from tests.integration import test_review_postgis as review

pytestmark = pytest.mark.integration

# The review tests' app and clean-up (items with extracted_by = 'test' go after each test).
review_app = review.review_app
_restore_staging = review._restore_staging
auth, insert_item = review.auth, review.insert_item


async def run_row(app, *, cost: float) -> tuple[int, int]:
    """A finished extraction run over document 2 with its job, as the worker leaves them."""
    async with app.state.session_factory() as session:
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO pipeline_jobs (municipality_id, kind, type, queue, status, "
                    "document_id, requested_by, target_type, target_id) VALUES ('podgorica', "
                    "'extract', 'extract_document', 'extraction', 'succeeded', 2, 'test', "
                    "'document', 2) RETURNING id"
                )
            )
        ).scalar_one()
        run_id = (
            await session.execute(
                text(
                    "INSERT INTO extraction_runs (municipality_id, document_id, lineage_id, "
                    "file_sha256, model, model_version, prompt_version, schema_version, status, "
                    "job_id, items_written, estimated_cost_eur, requested_by, finished_at) "
                    "VALUES ('podgorica', 2, 2, 'ab', 'claude-sonnet-5', 'claude-sonnet-5', '1.1', "
                    "'1.0', 'ready_for_review', :job, 4, :cost, 'test', now()) RETURNING id"
                ),
                {"job": job_id, "cost": cost},
            )
        ).scalar_one()
        await session.commit()
    return int(run_id), int(job_id)


async def staged(app, *, run_id: int | None = None, **cols) -> int:
    """A staged item (the review tests' helper), linked to a run when given."""
    item_id = await insert_item(app, **cols)
    if run_id is not None:
        async with app.state.session_factory() as session:
            await session.execute(
                text("UPDATE planning_parameter_extractions SET run_id = :r WHERE id = :i"),
                {"r": run_id, "i": item_id},
            )
            await session.commit()
    return item_id


async def drop_run(app, run_id: int, job_id: int) -> None:
    async with app.state.session_factory() as session:
        await session.execute(
            text("DELETE FROM planning_parameter_extractions WHERE run_id = :r"), {"r": run_id}
        )
        await session.execute(text("DELETE FROM extraction_runs WHERE id = :r"), {"r": run_id})
        await session.execute(text("DELETE FROM pipeline_jobs WHERE id = :j"), {"j": job_id})
        await session.commit()


async def test_sort_orders_run_block_and_options(review_app):
    app = review_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        run_id, job_id = await run_row(app, cost=0.4213)
        try:
            p9 = await staged(
                app,
                source_page=9,
                confidence=0.95,
                run_id=run_id,
                field_key="max_far",
                value_number=2.0,
            )
            p3_low = await staged(
                app,
                source_page=3,
                confidence=0.41,
                run_id=run_id,
                field_key="max_floors",
                value_number=None,
                value_text="P+5+Pk",
            )
            p3_done = await staged(
                app,
                source_page=3,
                confidence=0.99,
                review_state="approved",
                run_id=run_id,
                field_key="max_site_coverage_pct",
                value_number=55,
                unit="%",
            )
            uses = [
                await staged(
                    app,
                    source_page=4,
                    field_key="land_use",
                    value_number=None,
                    value_text=wording,
                    review_state=state,
                )
                for wording, state in (
                    ("Stanovanje male gustine", "pending_review"),
                    ("Stanovanje male gustine", "approved"),
                    ("Mješovita namjena", "pending_review"),
                    ("Misread wording", "rejected"),
                )
            ]
            ours = {p9, p3_low, p3_done, *uses}

            async def ids(**params) -> list[int]:
                answer = await client.get(
                    "/v1/admin/review",
                    params={"document_id": 2, "limit": 200, **params},
                    headers=auth(),
                )
                assert answer.status_code == 200, answer.text
                return [i["id"] for i in answer.json()["items"] if i["id"] in ours]

            pending_first = await ids()
            by_page = await ids(sort="page")
            by_confidence = await ids(sort="confidence")
            item = (await client.get(f"/v1/admin/review/{p3_low}", headers=auth())).json()
            coverage = (await client.get(f"/v1/admin/review/{p3_done}", headers=auth())).json()
            options = await client.get(
                "/v1/admin/review/options",
                params={"document_id": 2, "field_key": "land_use"},
                headers=auth(),
            )
            bad_field = await client.get(
                "/v1/admin/review/options",
                params={"document_id": 2, "field_key": "DROP TABLE"},
                headers=auth(),
            )
        finally:
            await drop_run(app, run_id, job_id)

    # pending first, each group by page; page order ignores the status; confidence: lowest first
    assert pending_first.index(p3_low) < pending_first.index(p9) < pending_first.index(p3_done)
    assert by_page.index(p3_low) < by_page.index(p9) and by_page.index(p3_done) < by_page.index(p9)
    assert by_confidence[0] == p3_low

    assert item["run"] == {
        "id": run_id,
        "job_id": job_id,
        "model_version": "claude-sonnet-5",
        "estimated_cost_eur": 0.4213,
        "items_written": 4,
        "finished_at": item["run"]["finished_at"],
    }
    assert coverage["field_unit"] == "%" and coverage["extracted"]["unit"] == "%"

    assert options.status_code == 200
    assert options.json()["values"] == [
        {"value": "Stanovanje male gustine", "count": 2},
        {"value": "Mješovita namjena", "count": 1},
    ]
    assert bad_field.status_code == 422
