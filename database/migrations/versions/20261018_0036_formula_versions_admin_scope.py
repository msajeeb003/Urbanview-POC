"""Formula versions; drop admin job types and e-mail columns the POC plan does not fund

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-18

The conformance pass of the order flow and the admin panel (2026-09-30):

- ``formula_versions`` (the pilot scope's ``public.formula_version``; the A5 row's "formula version
  display"): product-wide rows (the engine knows no municipality) with the label the engine
  states (``FORMULA_VERSION``), the date it applies from, ``is_current`` (at most one), the
  approval note and the engine package that implements it. Seeded with ``poc-1``, the formula
  since the panel schema (0003, 2026-09-23), whose fixtures still await the client's validation
  (P0 gate 3). A new formula is a new engine release and a new row, never an edit.
- the ``ai_check`` job type (0028; the AI settings page was removed on 2026-09-28) and the
  ``preprocess_file`` job type (its trigger route is gone; the PDF stage runs inside the
  extraction and geometry jobs). Jobs of both types are deleted (every reference to a job row is
  ON DELETE SET NULL). Table ``app_secrets`` stays for now: it holds the Anthropic key saved on
  2026-09-28 (encrypted, read by nothing); it goes once the key lives in the server settings.
- ``email_log.bounce_reason`` / ``bounced_at`` and the ``bounced`` status: the bounce endpoint was
  removed on 2026-09-29 and nothing sets them (provider webhooks are not in the POC plan); a
  ``bounced`` row, if any, becomes ``failed``.

A downgrade drops ``formula_versions`` and restores the columns, CHECKs and comments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPES_AFTER = (
    "'extract_document', 'process_geometry', 'publish_approved', 'send_email', "
    "'import_market_data', 'refresh_heatmaps', 'import_zones'"
)
JOB_TYPES_BEFORE = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', "
    "'send_email', 'import_market_data', 'refresh_heatmaps', 'ai_check', 'import_zones'"
)
TYPE_COMMENT_AFTER = (
    "extract_document | process_geometry | publish_approved | send_email | import_market_data"
    " | refresh_heatmaps | import_zones"
)
TYPE_COMMENT_BEFORE = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
    " | import_market_data | refresh_heatmaps | ai_check | import_zones"
)
TARGET_COMMENT_AFTER = "document | file | publish_run | email | market_import"
TARGET_COMMENT_BEFORE = TARGET_COMMENT_AFTER + " | ai_settings"
EMAIL_STATUSES_AFTER = "'queued', 'sent', 'suppressed', 'failed'"
EMAIL_STATUSES_BEFORE = EMAIL_STATUSES_AFTER + ", 'bounced'"
EMAIL_STATUS_COMMENT_AFTER = "queued | sent | suppressed | failed"
EMAIL_STATUS_COMMENT_BEFORE = EMAIL_STATUS_COMMENT_AFTER + " | bounced"
POC_1_NOTE = (
    "The POC plan's formulas (GFA, coverage area, profit, ROI; pessimistic range pairing). "
    "Fixtures await the client's validation (P0 gate 3)."
)


def _job_types(types: str, comment: str, previous_comment: str) -> None:
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint("ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({types})")
    op.alter_column(
        "pipeline_jobs",
        "type",
        comment=comment,
        existing_comment=previous_comment,
        existing_type=sa.Text(),
        existing_nullable=False,
    )


def _email_statuses(statuses: str, comment: str, previous_comment: str) -> None:
    op.drop_constraint("ck_email_log_status", "email_log", type_="check")
    op.create_check_constraint("ck_email_log_status", "email_log", f"status IN ({statuses})")
    op.alter_column(
        "email_log",
        "status",
        comment=comment,
        existing_comment=previous_comment,
        existing_type=sa.Text(),
        existing_nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "formula_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "label", sa.Text(), nullable=False, comment="what the engine states (FORMULA_VERSION)"
        ),
        sa.Column("effective_from", sa.Date(), nullable=False, comment="applies from this date"),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="the formula the engine runs; at most one row",
        ),
        sa.Column("approval_note", sa.Text(), nullable=True, comment="the client's approval"),
        sa.Column(
            "engine_package",
            sa.Text(),
            nullable=False,
            comment="the shared engine package that implements it",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("uq_formula_versions_label", "formula_versions", ["label"], unique=True)
    op.create_index(
        "uq_formula_versions_current",
        "formula_versions",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.execute(
        sa.text(
            "INSERT INTO formula_versions (label, effective_from, is_current, approval_note, "
            "engine_package) VALUES ('poc-1', DATE '2026-09-23', true, :note, "
            "'@urbanview/feasibility-engine')"
        ).bindparams(note=POC_1_NOTE)
    )

    op.execute("DELETE FROM pipeline_jobs WHERE type IN ('ai_check', 'preprocess_file')")
    _job_types(JOB_TYPES_AFTER, TYPE_COMMENT_AFTER, TYPE_COMMENT_BEFORE)
    op.alter_column(
        "pipeline_jobs",
        "target_type",
        comment=TARGET_COMMENT_AFTER,
        existing_comment=TARGET_COMMENT_BEFORE,
        existing_type=sa.Text(),
        existing_nullable=True,
    )

    op.execute("UPDATE email_log SET status = 'failed' WHERE status = 'bounced'")
    _email_statuses(EMAIL_STATUSES_AFTER, EMAIL_STATUS_COMMENT_AFTER, EMAIL_STATUS_COMMENT_BEFORE)
    op.drop_column("email_log", "bounced_at")
    op.drop_column("email_log", "bounce_reason")


def downgrade() -> None:
    op.add_column("email_log", sa.Column("bounce_reason", sa.Text(), nullable=True))
    op.add_column(
        "email_log", sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True)
    )
    _email_statuses(EMAIL_STATUSES_BEFORE, EMAIL_STATUS_COMMENT_BEFORE, EMAIL_STATUS_COMMENT_AFTER)

    op.alter_column(
        "pipeline_jobs",
        "target_type",
        comment=TARGET_COMMENT_BEFORE,
        existing_comment=TARGET_COMMENT_AFTER,
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    _job_types(JOB_TYPES_BEFORE, TYPE_COMMENT_BEFORE, TYPE_COMMENT_AFTER)

    op.drop_index("uq_formula_versions_current", table_name="formula_versions")
    op.drop_index("uq_formula_versions_label", table_name="formula_versions")
    op.drop_table("formula_versions")
