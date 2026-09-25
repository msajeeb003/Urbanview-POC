"""Schemas of the e-mail log (``/v1/admin/email-log``) and the magic-link login (``/v1/auth``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EmailStatus = Literal["queued", "sent", "suppressed", "failed", "bounced"]
EmailTemplate = Literal["payment_instructions", "order_delivered", "magic_link"]


class EmailLogOut(BaseModel):
    """One send; bodies are never stored, the subject is."""

    id: int
    template: EmailTemplate
    to_email: str
    order_id: int | None = None
    user_id: int | None = None
    status: EmailStatus
    attempts: int = 0
    subject: str | None = None
    provider_message_id: str | None = Field(
        default=None, description="The id the provider reported, else our Message-ID"
    )
    error: str | None = None
    suppressed_reason: str | None = None
    bounce_reason: str | None = None
    job_id: int | None = None
    created_at: datetime
    sent_at: datetime | None = None
    bounced_at: datetime | None = None
    updated_at: datetime | None = None


class EmailLogList(BaseModel):
    items: list[EmailLogOut]
    total: int
    limit: int
    offset: int


class BounceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000, description="What the provider reported")


class MagicLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _looks_like_an_email(cls, value: str) -> str:
        value = value.strip().lower()
        local, _, domain = value.partition("@")
        if not local or "." not in domain or " " in value:
            raise ValueError("must be an e-mail address")
        return value


class MagicLinkAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"
    message_en: str
    message_me: str


class MagicLinkExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)


class SessionUser(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    role: str


class SessionOut(BaseModel):
    token: str = Field(description="Bearer token for the staff routes; shown once")
    expires_at: datetime
    user: SessionUser
