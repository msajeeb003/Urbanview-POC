"""The admin API holds what the POC plan funds: routes removed as unfunded stay gone (the
Overview dashboard, bulk approval, the e-mail log and job-cost listings, the separate PDF
pre-processing trigger) and the formula versions of the A5 screen are there."""

from __future__ import annotations

from tests.helpers import make_app

REMOVED = {
    ("get", "/v1/admin/overview"),
    ("get", "/v1/admin/email-log"),
    ("get", "/v1/admin/email-log/{log_id}"),
    ("get", "/v1/admin/jobs/costs"),
    ("post", "/v1/admin/review/bulk-approve"),
    ("post", "/v1/admin/geometry/bulk-approve"),
    ("post", "/v1/admin/files/{file_id}/jobs/preprocess"),
}


def test_unfunded_admin_routes_are_gone():
    paths = make_app().openapi()["paths"]
    present = {(method, path) for path, operations in paths.items() for method in operations}
    assert REMOVED.isdisjoint(present)
    assert ("get", "/v1/admin/formulas") in present
