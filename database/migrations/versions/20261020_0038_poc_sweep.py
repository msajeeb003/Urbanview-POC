"""The 13 analytics events, no app_secrets, validity-only geometry QA wording

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-20

The analytics / audit brief and the whole-repo excess sweep (2026-09-30) against the 220 h POC
plan:

- ``analytics_events.name`` takes the 13 events: BRD §6.2's eleven and the pilot scope's two
  intent buttons. ``assumption_edited`` (0029) is not in the plan's list. The table is
  append-only (0030), so rows with that name are never deleted: when any exist the check is
  added ``NOT VALID`` (it holds for new rows); the server had none.
- ``app_secrets`` (0028: the removed AI settings page's table, read by nothing since 2026-09-28)
  is dropped: the worker reads ``ANTHROPIC_API_KEY`` from the server's settings.
- ``planning_documents.page_images_rendered`` (0004) is dropped with the page images: they were
  rendered but only served behind a setting that was never on; the source viewer and the review
  queue always open the PDF at the cited page. Stored PDF-stage manifests
  (``stored_files.preprocess``) lose the ``page_images`` and ``ocr_pages`` keys (OCR is gone too),
  so they still load.
- ``geometry_batches.qa_status`` / ``qa_issues`` say what the QA is since the conformance pass of
  2026-09-29 (0035): geometry validity and the producing run's warnings, no topology QA; the
  ``layer_id`` comments of ``geometry_batches`` / ``layer_features`` lose ``traffic_network`` (the
  planned traffic network is not extracted any more) and the market ``normaliser`` comments lose
  the removed LLM step (rules only since 2026-09-30).
- The PostGIS image's ``tiger`` / ``tiger_data`` / ``topology`` schemas that 0037's extension
  drop left behind are dropped when they are empty.

A downgrade restores the fourteen-name check, the comments, an empty ``app_secrets`` and the
``page_images_rendered`` column (false); the manifests' removed keys are not restored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENTS = (
    "map_loaded",
    "search_performed",
    "parcel_selected",
    "layer_toggled",
    "panel_viewed",
    "financials_viewed",
    "source_reference_opened",
    "order_started",
    "checkout_completed",
    "return_visit",
    "sessions_per_user",
    "market_data_interest",
    "ai_interest",
)
EVENTS_BEFORE = (*EVENTS, "assumption_edited")

# (table, column, type, nullable, comment after, comment before)
COMMENTS = (
    (
        "geometry_batches",
        "layer_id",
        sa.Text(),
        False,
        "cadastral_parcels | cadastral_municipalities | urban_parcels | urban_blocks | zones "
        "| document_coverage | land_use",
        "cadastral_parcels | cadastral_municipalities | urban_parcels | urban_blocks | zones "
        "| document_coverage | land_use | traffic_network",
    ),
    ("layer_features", "layer_id", sa.Text(), False, "land_use", "land_use | traffic_network"),
    (
        "market_imports",
        "normaliser",
        sa.Text(),
        True,
        "rules (the profile's mapping rules)",
        "rules, or llm:<model> with the prompt version",
    ),
    ("market_data", "normaliser", sa.Text(), False, "rules", "rules or llm:<model>"),
)
# (column, type, nullable, comment after, comment before)
QA_COMMENTS = (
    (
        "qa_status",
        sa.Text(),
        True,
        "validity QA (core.geometry_qa): pass | warn | fail; null = not checked yet",
        "topology QA (core.geometry_qa): pass | warn | fail; null = not checked yet",
    ),
    (
        "qa_issues",
        postgresql.JSONB(astext_type=sa.Text()),
        False,
        "invalid or empty geometry, the producing run's warnings",
        "overlaps, gaps, area deviation, invalid geometry, the dataset's warnings",
    ),
)
IMAGE_SCHEMAS = ("tiger_data", "tiger", "topology")


def _names(names: Sequence[str]) -> str:
    return "name IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def _comments(after: bool) -> None:
    for table, column, type_, nullable, comment_after, comment_before in (
        *COMMENTS,
        *(("geometry_batches", *row) for row in QA_COMMENTS),
    ):
        op.alter_column(
            table,
            column,
            existing_type=type_,
            existing_nullable=nullable,
            comment=comment_after if after else comment_before,
            existing_comment=comment_before if after else comment_after,
        )


def upgrade() -> None:
    op.drop_constraint("ck_analytics_events_name", "analytics_events", type_="check")
    stale = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM analytics_events WHERE name = 'assumption_edited'"))
        .scalar_one()
    )
    op.execute(
        "ALTER TABLE analytics_events ADD CONSTRAINT ck_analytics_events_name "
        f"CHECK ({_names(EVENTS)})" + (" NOT VALID" if stale else "")
    )
    op.drop_index("uq_app_secrets_name", table_name="app_secrets")
    op.drop_table("app_secrets")
    op.drop_column("planning_documents", "page_images_rendered")
    op.execute(
        "UPDATE stored_files SET preprocess = (preprocess - 'page_images') "
        "#- '{summary,page_images}' #- '{summary,ocr_pages}' WHERE preprocess IS NOT NULL"
    )
    _comments(after=True)
    for schema in IMAGE_SCHEMAS:
        # RESTRICT: a schema that still holds anything stays
        op.execute(
            f"""
            DO $$ BEGIN
              EXECUTE 'DROP SCHEMA IF EXISTS {schema}';
            EXCEPTION WHEN dependent_objects_still_exist THEN NULL;
            END $$
            """
        )


def downgrade() -> None:
    _comments(after=False)
    op.add_column(
        "planning_documents",
        sa.Column(
            "page_images_rendered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="page images at <municipality>/planning-documents/<id>/pages/NNNN.png",
        ),
    )
    op.create_table(
        "app_secrets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="anthropic_api_key"),
        sa.Column(
            "ciphertext",
            sa.Text(),
            nullable=False,
            comment="Fernet token under SECRETS_ENCRYPTION_KEY; never returned, logged or audited",
        ),
        sa.Column(
            "last4",
            sa.Text(),
            nullable=False,
            comment="the last 4 characters: the only part ever shown",
        ),
        sa.Column("set_by", sa.Text(), nullable=False, comment="principal subject"),
        sa.Column(
            "set_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "set_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="when the value was saved; a connection test names the value it used by it",
        ),
        sa.CheckConstraint("name IN ('anthropic_api_key')", name="ck_app_secrets_name"),
        sa.CheckConstraint("char_length(last4) = 4", name="ck_app_secrets_last4"),
    )
    op.create_index("uq_app_secrets_name", "app_secrets", ["municipality_id", "name"], unique=True)
    op.drop_constraint("ck_analytics_events_name", "analytics_events", type_="check")
    op.create_check_constraint(
        "ck_analytics_events_name", "analytics_events", _names(EVENTS_BEFORE)
    )
