"""Review queue pieces that need no database: payload validation (notes trimmed, a reason
required), status mapping, the publish rule, the staged payload on an item and the signed page
link helper. The correction rules: ``tests/test_corrections_unit.py``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from api.schemas.review import AmendIn, ApproveIn, RejectIn
from api.services.review import DB_TO_STATUS, STATUS_TO_DB, _payload_out, can_publish
from api.services.source import signed_page_link


def test_status_mapping_round_trips():
    assert STATUS_TO_DB == {
        "pending": "pending_review",
        "approved": "approved",
        "amended": "amended",
        "rejected": "rejected",
    }
    assert {DB_TO_STATUS[v]: v for v in STATUS_TO_DB.values()} == STATUS_TO_DB


def test_amend_payload():
    assert AmendIn(value=3.4, note="table 3").value == 3.4
    assert AmendIn(value=" mixed use ", note="x").value == "mixed use"
    amend = AmendIn(value="27.5", unit="m", note="  table 3  ")
    assert (amend.unit, amend.note, amend.confirm_out_of_range) == ("m", "table 3", False)
    assert AmendIn(value=99, note="plan", confirm_out_of_range=True).confirm_out_of_range
    bad_payloads = (
        {"value": True, "note": "x"},
        {"value": "", "note": "x"},
        {"value": "   ", "note": "x"},
        {"value": 1, "note": "x", "extra": 1},
        {"value": 1},  # the note is required
        {"value": 1, "note": ""},
        {"value": 1, "note": "   "},  # a note of spaces is no note
        {},
    )
    for bad in bad_payloads:
        with pytest.raises(ValidationError):
            AmendIn(**bad)
    with pytest.raises(ValidationError):
        AmendIn(value=1, unit="x" * 31, note="x")


def test_approve_and_reject_payloads():
    assert ApproveIn().note is None
    assert ApproveIn(note="ok").note == "ok"
    assert ApproveIn(note="   ").note is None  # an optional note of spaces is no note
    with pytest.raises(ValidationError):
        ApproveIn(value=1)
    assert RejectIn(note="  wrong table ").note == "wrong table"
    for bad in ({}, {"note": ""}, {"note": "   "}, {"note": "\n\t"}):
        with pytest.raises(ValidationError):
            RejectIn(**bad)


def test_an_item_carries_its_staged_payload():
    raw = {
        "schema_version": "1.0",
        "prompt_version": "1.1",
        "task": "parameter_table",
        "entity_type": "urban_parcel",
        "path": "urban_parcels[UP 12].rules.max_site_coverage_pct",
        "field_key": "max_site_coverage_pct",
        "urban_parcel_number": "UP 12",
        "leaf": {
            "value": 40.0,
            "unit": "%",
            "raw_text": "0,40",
            "source": {
                "document_id": 6,
                "page": 1,
                "bbox": [384.0, 674.0, 454.0, 696.0],
                "table_ref": {"table": "p1t1", "row": "UP 12", "column": "IZ", "cell": "r2c5"},
            },
            "confidence": 0.95,
            "extraction_method": "table",
            "stated": {"value": "0,40", "unit": "ratio"},
            "normalisation": ["decimal_comma", "ratio_to_percent"],
            "flags": [],
        },
    }
    payload = _payload_out({"id": 5, "schema_version": "1.0", "payload": raw})
    assert payload is not None
    assert (payload.stated_value, payload.stated_unit, payload.value, payload.unit) == (
        "0,40",
        "ratio",
        40.0,
        "%",
    )
    assert payload.normalisation == ["decimal_comma", "ratio_to_percent"]
    assert payload.table is not None and payload.table.cell == "r2c5"
    assert payload.task == "parameter_table" and payload.urban_parcel_number == "UP 12"
    # manual / seeded items have none; an unreadable one lists without it
    assert _payload_out({"id": 1, "schema_version": None, "payload": None}) is None
    assert _payload_out({"id": 2, "schema_version": "9.0", "payload": raw}) is None


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
