"""Review queue pieces that need no database: payload validation, status mapping, typed
corrections, the publish rule and the signed page link helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from api.schemas.review import AmendIn, ApproveIn, BulkApproveIn, RejectIn
from api.services.review import DB_TO_STATUS, STATUS_TO_DB, _typed_correction, can_publish
from api.services.source import signed_page_link
from core.errors import AppError


def test_status_mapping_round_trips():
    assert STATUS_TO_DB == {
        "pending": "pending_review",
        "approved": "approved",
        "amended": "amended",
        "rejected": "rejected",
    }
    assert {DB_TO_STATUS[v]: v for v in STATUS_TO_DB.values()} == STATUS_TO_DB


def test_amend_payload():
    assert AmendIn(value=3.4).value == 3.4
    assert AmendIn(value=" mixed use ").value == "mixed use"
    assert AmendIn(value="27.5", unit="m", note="table 3").unit == "m"
    bad_payloads = ({"value": True}, {"value": ""}, {"value": "   "}, {"value": 1, "extra": 1}, {})
    for bad in bad_payloads:
        with pytest.raises(ValidationError):
            AmendIn(**bad)
    with pytest.raises(ValidationError):
        AmendIn(value=1, unit="x" * 31)


def test_approve_and_reject_payloads():
    assert ApproveIn().note is None
    assert ApproveIn(note="ok").note == "ok"
    with pytest.raises(ValidationError):
        ApproveIn(value=1)
    assert RejectIn(note="wrong table").note == "wrong table"
    for bad in ({}, {"note": ""}):
        with pytest.raises(ValidationError):
            RejectIn(**bad)


def test_bulk_approve_needs_a_selector():
    for bad in ({}, {"source_page": 3}, {"item_ids": [0]}, {"item_ids": []}, {"note": "x"}):
        with pytest.raises(ValidationError):
            BulkApproveIn(**bad)
    assert BulkApproveIn(item_ids=[1, 2]).item_ids == [1, 2]
    assert BulkApproveIn(document_id=2, source_page=13).source_page == 13
    assert BulkApproveIn(urban_parcel_id=1).urban_parcel_id == 1


def test_typed_corrections_follow_the_parameter_type():
    assert _typed_correction("number", 3.4) == (None, 3.4)
    assert _typed_correction("number", "3,4") == (None, 3.4)
    assert _typed_correction("text", "Stanovanje") == ("Stanovanje", None)
    with pytest.raises(AppError) as number_error:
        _typed_correction("number", "twenty")
    assert number_error.value.status_code == 422
    with pytest.raises(AppError) as text_error:
        _typed_correction("text", 5)
    assert text_error.value.status_code == 422


def test_publish_rule():
    assert can_publish(0, 1, 0) == (True, [])
    assert can_publish(0, 0, 2) == (True, [])
    ok, blockers = can_publish(1, 3, 0)
    assert not ok and blockers == ["1 item(s) pending review"]
    ok, blockers = can_publish(0, 0, 0)
    assert not ok and blockers == ["nothing approved or amended"]
    ok, blockers = can_publish(2, 0, 0)
    assert not ok and len(blockers) == 2


class FakeStorage:
    def presigned_get_url(self, key, expires_in=900, *, content_type=None, inline=False):
        return f"https://minio.test/b/{key}?exp={expires_in}"


def test_signed_page_link():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    common = {"document_id": 2, "expires_in_seconds": 600, "now": now}
    image = signed_page_link(
        FakeStorage(),
        "podgorica",
        file_key="podgorica/planning-documents/2/document.pdf",
        page_count=24,
        page_images_rendered=True,
        page=12,
        **common,
    )
    assert image == {
        "url": "https://minio.test/b/podgorica/planning-documents/2/pages/0012.png?exp=600",
        "kind": "page_image",
        "content_type": "image/png",
        "expires_at": now + timedelta(seconds=600),
    }
    pdf = signed_page_link(
        FakeStorage(),
        "podgorica",
        file_key="podgorica/uploads/planning_document/abc/plan.pdf",
        page_count=None,
        page_images_rendered=True,  # ignored without a known page count
        page=7,
        **common,
    )
    assert pdf["kind"] == "pdf_page"
    assert pdf["url"].endswith("plan.pdf?exp=600#page=7")
    none_cases = [
        {"file_key": None, "page_count": 3, "page_images_rendered": False, "page": 1},
        {"file_key": "k", "page_count": 3, "page_images_rendered": False, "page": 4},
        {"file_key": "k", "page_count": 3, "page_images_rendered": False, "page": None},
        {"file_key": "k", "page_count": None, "page_images_rendered": False, "page": 0},
    ]
    for case in none_cases:
        assert signed_page_link(FakeStorage(), "podgorica", **case, **common) is None
