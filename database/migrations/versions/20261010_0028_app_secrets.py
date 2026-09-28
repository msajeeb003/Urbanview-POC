"""app_secrets: secrets saved from the admin console; the ai_check job type

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-10

The admin console's AI extraction page stores the Anthropic API key when the server environment
does not set one: ``app_secrets`` holds one row per municipality and secret name, the value
encrypted with Fernet under ``SECRETS_ENCRYPTION_KEY`` (``core.app_secrets``) and only its last
four characters in the clear (the only part ever shown). The worker decrypts it when a job needs
it; the API never returns, logs or audits it. ``ai_check`` becomes a job type: the connection
test the worker runs with the resolved key (target type ``ai_settings``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPES_BEFORE = (
    "'extract_document', 'preprocess_file', 'process_geometry', 'publish_approved', "
    "'send_email', 'import_market_data', 'refresh_heatmaps'"
)
JOB_TYPES_AFTER = JOB_TYPES_BEFORE + ", 'ai_check'"
TYPE_COMMENT_BEFORE = (
    "extract_document | preprocess_file | process_geometry | publish_approved | send_email"
    " | import_market_data | refresh_heatmaps"
)
TYPE_COMMENT_AFTER = TYPE_COMMENT_BEFORE + " | ai_check"
TARGET_COMMENT_BEFORE = "document | file | publish_run | email | market_import"
TARGET_COMMENT_AFTER = TARGET_COMMENT_BEFORE + " | ai_settings"


def upgrade() -> None:
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
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_AFTER})"
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        comment=TYPE_COMMENT_AFTER,
        existing_comment=TYPE_COMMENT_BEFORE,
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "pipeline_jobs",
        "target_type",
        comment=TARGET_COMMENT_AFTER,
        existing_comment=TARGET_COMMENT_BEFORE,
        existing_type=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM pipeline_jobs WHERE type = 'ai_check'")
    op.alter_column(
        "pipeline_jobs",
        "target_type",
        comment=TARGET_COMMENT_BEFORE,
        existing_comment=TARGET_COMMENT_AFTER,
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "pipeline_jobs",
        "type",
        comment=TYPE_COMMENT_BEFORE,
        existing_comment=TYPE_COMMENT_AFTER,
        existing_type=sa.Text(),
        existing_nullable=False,
    )
    op.drop_constraint("ck_pipeline_jobs_type", "pipeline_jobs", type_="check")
    op.create_check_constraint(
        "ck_pipeline_jobs_type", "pipeline_jobs", f"type IN ({JOB_TYPES_BEFORE})"
    )
    op.drop_index("uq_app_secrets_name", table_name="app_secrets")
    op.drop_table("app_secrets")
