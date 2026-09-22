"""panel schema: publish versions, planning field dictionary, planning parameter values (serving)
and extractions (staging), financial assumptions, planning_documents.amends_document_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

Contract: docs/specs/panel-payload.md section 2. The public panel reads the serving tables only
(``planning_parameter_values`` + ``financial_assumptions`` + ``publish_versions``); the review
queue (``planning_parameter_extractions``) never reaches the map except through the publish job.
Every table except the product-wide ``planning_fields`` dictionary carries municipality_id,
dataset_version and created_at like the location tables of 0002. The dictionary rows are seeded
here (the seed loader never touches them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEW_STATE_ENUM_NAME = "review_state"
REVIEW_STATE_VALUES = ("pending_review", "approved", "rejected", "amended")

PLANNING_FIELDS = sa.table(
    "planning_fields",
    sa.column("key", sa.Text()),
    sa.column("field_group", sa.Text()),
    sa.column("sort_order", sa.Integer()),
    sa.column("label_en", sa.Text()),
    sa.column("label_me", sa.Text()),
    sa.column("abbreviation", sa.Text()),
    sa.column("unit", sa.Text()),
    sa.column("value_type", sa.Text()),
    sa.column("computed", sa.Boolean()),
    sa.column("formula", sa.Text()),
)


def _field(
    sort_order: int,
    key: str,
    label_en: str,
    label_me: str,
    *,
    abbreviation: str | None = None,
    unit: str | None = None,
    value_type: str = "number",
    computed: bool = False,
    formula: str | None = None,
) -> dict:
    return {
        "key": key,
        "field_group": "planning",
        "sort_order": sort_order,
        "label_en": label_en,
        "label_me": label_me,
        "abbreviation": abbreviation,
        "unit": unit,
        "value_type": value_type,
        "computed": computed,
        "formula": formula,
    }


# The 11 fields of the Group 1 ticket followed by the two engine-computed ones. Montenegrin labels
# are provisional (client to confirm).
PLANNING_FIELD_ROWS = [
    _field(10, "land_use", "Land use designation", "Namjena površina", value_type="text"),
    _field(
        20,
        "max_site_coverage_pct",
        "Max site coverage",
        "Maksimalni indeks zauzetosti",
        abbreviation="IZ",
        unit="%",
    ),
    _field(
        30, "max_far", "Max floor area ratio", "Maksimalni indeks izgrađenosti", abbreviation="II"
    ),
    _field(40, "max_height_m", "Max building height", "Maksimalna visina objekta", unit="m"),
    _field(50, "max_floors", "Max number of floors", "Maksimalna spratnost", value_type="text"),
    _field(
        60,
        "building_line_m",
        "Building line (setback from public area)",
        "Građevinska linija",
        unit="m",
    ),
    _field(
        70,
        "setback_neighbours_m",
        "Min distance from neighbouring parcels",
        "Minimalna udaljenost od susjednih parcela",
        unit="m",
    ),
    _field(80, "parking_requirement", "Parking requirement", "Parkiranje", value_type="text"),
    _field(90, "min_green_area_pct", "Min green area", "Minimalna zelena površina", unit="%"),
    _field(
        100,
        "planned_parcel_area_m2",
        "Planned parcel area (per plan)",
        "Površina urbanističke parcele",
        unit="m²",
    ),
    _field(
        110,
        "utilities",
        "Infrastructure utilities",
        "Infrastrukturna opremljenost",
        value_type="text",
    ),
    _field(
        200,
        "max_gfa_m2",
        "Calculated max gross floor area",
        "Maksimalna bruto građevinska površina",
        abbreviation="BGP",
        unit="m²",
        computed=True,
        formula="max_far × basis_area_m2",
    ),
    _field(
        210,
        "max_coverage_area_m2",
        "Max coverage area",
        "Maksimalna površina pod objektom",
        unit="m²",
        computed=True,
        formula="max_site_coverage_pct / 100 × basis_area_m2",
    ),
]


def created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    review_state = postgresql.ENUM(
        *REVIEW_STATE_VALUES, name=REVIEW_STATE_ENUM_NAME, create_type=False
    )
    review_state.create(op.get_bind(), checkfirst=True)

    # --- 2.1 publish_versions --------------------------------------------------------------
    op.create_table(
        "publish_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, comment="e.g. 2026-09-22.1"),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_by", sa.Text()),
        sa.Column("formula_version", sa.Text(), nullable=False, server_default=sa.text("'poc-1'")),
        sa.Column("notes", sa.Text()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
        sa.UniqueConstraint("municipality_id", "label", name="uq_publish_versions_label"),
    )
    op.create_index("ix_publish_versions_municipality_id", "publish_versions", ["municipality_id"])
    op.create_index(
        "uq_publish_versions_current",
        "publish_versions",
        ["municipality_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    # --- 2.2 planning_fields (product-wide dictionary, seeded here) ---------------------------
    op.create_table(
        "planning_fields",
        sa.Column("key", sa.Text(), primary_key=True, comment="snake_case"),
        sa.Column("field_group", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("label_en", sa.Text(), nullable=False),
        sa.Column("label_me", sa.Text(), nullable=False, comment="provisional (client to confirm)"),
        sa.Column("abbreviation", sa.Text(), comment="IZ / II / BGP"),
        sa.Column("unit", sa.Text(), comment="% / m / m²"),
        sa.Column("value_type", sa.Text(), nullable=False, comment="text | number"),
        sa.Column(
            "computed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="computed by the engine, never stored",
        ),
        sa.Column("formula", sa.Text(), comment="human-readable, for computed fields"),
    )
    op.bulk_insert(PLANNING_FIELDS, PLANNING_FIELD_ROWS)

    # --- 2.6 planning_documents.amends_document_id --------------------------------------------
    op.add_column(
        "planning_documents",
        sa.Column(
            "amends_document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="SET NULL"),
            comment="explicit amendment link set at ingestion (never by coverage)",
        ),
    )
    op.create_index(
        "ix_planning_documents_amends_document_id", "planning_documents", ["amends_document_id"]
    )

    # (id, document_id) must be a key so a parcel-level value can only cite its own document.
    op.create_unique_constraint(
        "uq_urban_parcels_id_document", "urban_parcels", ["id", "document_id"]
    )

    # --- 2.5 financial_assumptions ------------------------------------------------------------
    op.create_table(
        "financial_assumptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "zone_id",
            sa.BigInteger(),
            sa.ForeignKey("zones.id", ondelete="CASCADE"),
            comment="null = municipality-wide default",
        ),
        sa.Column(
            "land_rate_eur_m2",
            sa.Float(precision=53),
            nullable=False,
            comment="land value per m² of parcel area",
        ),
        sa.Column(
            "build_rate_eur_m2",
            sa.Float(precision=53),
            nullable=False,
            comment="construction cost per m² GFA",
        ),
        sa.Column(
            "design_rate_eur_m2",
            sa.Float(precision=53),
            nullable=False,
            comment="design & documentation per m² GFA",
        ),
        sa.Column(
            "sale_rate_eur_m2",
            sa.Float(precision=53),
            nullable=False,
            comment="selling price per m² saleable area",
        ),
        sa.Column(
            "range_low_factor",
            sa.Float(precision=53),
            nullable=False,
            server_default=sa.text("0.86"),
        ),
        sa.Column(
            "range_high_factor",
            sa.Float(precision=53),
            nullable=False,
            server_default=sa.text("1.15"),
        ),
        sa.Column("source", sa.Text(), comment="e.g. Realitica, Estitor, Monstat"),
        sa.Column("source_date", sa.Date()),
        sa.Column("notes", sa.Text()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.Text()),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
        sa.CheckConstraint(
            "range_low_factor <= 1 AND range_high_factor >= 1 AND land_rate_eur_m2 > 0 AND "
            "build_rate_eur_m2 > 0 AND design_rate_eur_m2 > 0 AND sale_rate_eur_m2 > 0",
            name="ck_financial_assumptions_values",
        ),
    )
    op.create_index(
        "ix_financial_assumptions_municipality_id", "financial_assumptions", ["municipality_id"]
    )
    op.create_index(
        "uq_financial_assumptions_current_zone",
        "financial_assumptions",
        ["municipality_id", "zone_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND zone_id IS NOT NULL"),
    )
    op.create_index(
        "uq_financial_assumptions_current_default",
        "financial_assumptions",
        ["municipality_id"],
        unique=True,
        postgresql_where=sa.text("is_current AND zone_id IS NULL"),
    )

    # --- 2.3 planning_parameter_values (SERVING) ------------------------------------------------
    op.create_table(
        "planning_parameter_values",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
            comment="the document the value is cited from",
        ),
        sa.Column(
            "urban_parcel_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_parcels.id", ondelete="CASCADE"),
            comment="null = document-level value",
        ),
        sa.Column("field_key", sa.Text(), sa.ForeignKey("planning_fields.key"), nullable=False),
        sa.Column("value_text", sa.Text()),
        sa.Column("value_number", sa.Float(precision=53)),
        sa.Column("unit", sa.Text(), comment="override of the dictionary unit"),
        sa.Column(
            "source_page", sa.Integer(), nullable=False, comment="1-based page in the source PDF"
        ),
        sa.Column(
            "source_bbox",
            postgresql.JSONB(),
            comment="[x0, y0, x1, y1] in PDF points, origin bottom-left",
        ),
        sa.Column("source_note", sa.Text(), comment="e.g. table 3 – UP 12"),
        sa.Column(
            "publish_version_id",
            sa.BigInteger(),
            sa.ForeignKey("publish_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
        sa.CheckConstraint(
            "num_nonnulls(value_text, value_number) = 1",
            name="ck_planning_parameter_values_one_value",
        ),
        sa.ForeignKeyConstraint(
            ["urban_parcel_id", "document_id"],
            ["urban_parcels.id", "urban_parcels.document_id"],
            name="fk_planning_parameter_values_parcel_document",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_planning_parameter_values_municipality_id",
        "planning_parameter_values",
        ["municipality_id"],
    )
    op.create_index(
        "ix_planning_parameter_values_document_id", "planning_parameter_values", ["document_id"]
    )
    op.create_index(
        "ix_planning_parameter_values_publish_version_id",
        "planning_parameter_values",
        ["publish_version_id"],
    )
    # Also the parcel lookup path: no separate index on urban_parcel_id.
    op.create_index(
        "uq_planning_parameter_values_parcel",
        "planning_parameter_values",
        ["urban_parcel_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NOT NULL"),
    )
    op.create_index(
        "uq_planning_parameter_values_document",
        "planning_parameter_values",
        ["document_id", "field_key"],
        unique=True,
        postgresql_where=sa.text("urban_parcel_id IS NULL"),
    )

    # --- 2.4 planning_parameter_extractions (STAGING, never read by the public API) -------------
    op.create_table(
        "planning_parameter_extractions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("municipality_id", sa.Text(), nullable=False),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "urban_parcel_id",
            sa.BigInteger(),
            sa.ForeignKey("urban_parcels.id", ondelete="CASCADE"),
        ),
        sa.Column("field_key", sa.Text(), sa.ForeignKey("planning_fields.key"), nullable=False),
        sa.Column("value_text", sa.Text()),
        sa.Column("value_number", sa.Float(precision=53)),
        sa.Column("unit", sa.Text()),
        sa.Column("source_page", sa.Integer()),
        sa.Column("source_bbox", postgresql.JSONB()),
        sa.Column("source_note", sa.Text()),
        sa.Column("extracted_by", sa.Text(), nullable=False, comment="llm:<model> or manual"),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "review_state",
            review_state,
            nullable=False,
            server_default=sa.text("'pending_review'"),
        ),
        sa.Column("reviewer", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column(
            "published_value_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_parameter_values.id", ondelete="SET NULL"),
        ),
        sa.Column("dataset_version", sa.Text()),
        created_at(),
    )
    op.create_index(
        "ix_planning_parameter_extractions_municipality_id",
        "planning_parameter_extractions",
        ["municipality_id"],
    )
    op.create_index(
        "ix_planning_parameter_extractions_document_id",
        "planning_parameter_extractions",
        ["document_id"],
    )
    op.create_index(
        "ix_planning_parameter_extractions_review",
        "planning_parameter_extractions",
        ["municipality_id", "review_state"],
    )


def downgrade() -> None:
    op.drop_table("planning_parameter_extractions")
    op.drop_table("planning_parameter_values")
    op.drop_table("financial_assumptions")
    op.drop_constraint("uq_urban_parcels_id_document", "urban_parcels", type_="unique")
    op.drop_index("ix_planning_documents_amends_document_id", table_name="planning_documents")
    op.drop_column("planning_documents", "amends_document_id")
    op.drop_table("planning_fields")  # drops the seeded dictionary rows with it
    op.drop_table("publish_versions")
    postgresql.ENUM(name=REVIEW_STATE_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
