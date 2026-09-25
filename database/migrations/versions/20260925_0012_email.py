"""transactional e-mail: email_log carries the job, the provider message id, attempts, bounce
and suppression details; staff_login_tokens for magic-link login

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25

Every e-mail is a ``send_email`` job (``jobs/tasks/email.py``) over an ``email_log`` row: the
API inserts the row as ``queued`` and the worker renders, sends and records the outcome
(``sent`` with the provider's message id, ``failed`` with the error after the retries,
``suppressed`` with the policy reason, ``bounced`` when a bounce is reported). Bodies are never
stored. ``staff_login_tokens`` hold the hashed single-use tokens behind magic links.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "email_log"


def upgrade() -> None:
    op.alter_column(TABLE, "subject", existing_type=sa.Text(), nullable=True)
    op.alter_column(
        TABLE,
        "template",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="payment_instructions | order_delivered | magic_link",
        existing_comment="payment_instructions | report_delivered | …",
    )
    op.alter_column(
        TABLE,
        "status",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="queued | sent | suppressed | failed | bounced",
        existing_comment="sent | suppressed | failed",
    )
    op.drop_constraint("ck_email_log_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_email_log_status",
        TABLE,
        "status IN ('queued', 'sent', 'suppressed', 'failed', 'bounced')",
    )
    op.add_column(
        TABLE,
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="SET NULL"),
            nullable=True,
            comment="staff recipient (magic links)",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "provider_message_id",
            sa.Text(),
            nullable=True,
            comment="the id the SMTP provider reported, else our Message-ID",
        ),
    )
    op.add_column(
        TABLE,
        sa.Column("provider_response", sa.Text(), nullable=True, comment="the DATA reply"),
    )
    op.add_column(
        TABLE, sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    op.add_column(TABLE, sa.Column("suppressed_reason", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("bounce_reason", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_email_log_status", TABLE, ["municipality_id", "status"])
    op.create_index("ix_email_log_user", TABLE, ["user_id"])

    op.create_table(
        "staff_login_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("staff_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.Text(),
            nullable=False,
            comment="sha256 of the single-use token; the token itself only travels in the e-mail",
        ),
        sa.Column(
            "email_log_id",
            sa.BigInteger(),
            sa.ForeignKey("email_log.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_staff_login_tokens_token_hash", "staff_login_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_staff_login_tokens_user_id", "staff_login_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_login_tokens_user_id", table_name="staff_login_tokens")
    op.drop_index("uq_staff_login_tokens_token_hash", table_name="staff_login_tokens")
    op.drop_table("staff_login_tokens")
    op.drop_index("ix_email_log_user", table_name=TABLE)
    op.drop_index("ix_email_log_status", table_name=TABLE)
    for column in (
        "updated_at",
        "bounced_at",
        "sent_at",
        "bounce_reason",
        "suppressed_reason",
        "attempts",
        "provider_response",
        "provider_message_id",
        "job_id",
        "user_id",
    ):
        op.drop_column(TABLE, column)
    op.execute(f"DELETE FROM {TABLE} WHERE status IN ('queued', 'bounced') OR subject IS NULL")
    op.drop_constraint("ck_email_log_status", TABLE, type_="check")
    op.create_check_constraint(
        "ck_email_log_status", TABLE, "status IN ('sent', 'suppressed', 'failed')"
    )
    op.alter_column(
        TABLE,
        "status",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="sent | suppressed | failed",
        existing_comment="queued | sent | suppressed | failed | bounced",
    )
    op.alter_column(
        TABLE,
        "template",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment="payment_instructions | report_delivered | …",
        existing_comment="payment_instructions | order_delivered | magic_link",
    )
    op.alter_column(TABLE, "subject", existing_type=sa.Text(), nullable=False)
