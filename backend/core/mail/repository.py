"""What the ``send_email`` job reads and writes: the ``email_log`` row, the order or staff user
behind it (resolved at send time, so the job payload carries ids only) and the magic-link token.
``SqlEmailRepository`` for workers, ``MemoryEmailRepository`` for unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(slots=True)
class EmailLogRow:
    id: int
    municipality_id: str
    template: str
    to_email: str
    order_id: int | None
    user_id: int | None
    status: str
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class OrderMailFacts:
    id: int
    reference: str
    email: str
    first_name: str
    parcel_label: str
    document_name: str | None
    price_eur: float
    currency: str
    turnaround_business_days: int
    expected_by: date
    report_key: str | None


@dataclass(frozen=True, slots=True)
class StaffUserFacts:
    id: int
    email: str
    display_name: str | None
    role: str
    is_active: bool


class EmailRepository(Protocol):
    async def load_log(self, log_id: int) -> EmailLogRow | None: ...

    async def load_order(self, order_id: int) -> OrderMailFacts | None: ...

    async def load_staff_user(self, user_id: int) -> StaffUserFacts | None: ...

    async def create_login_token(
        self, user_id: int, token_hash: str, expires_at: datetime, email_log_id: int
    ) -> int: ...

    async def mark_attempt(self, log_id: int, error: str, subject: str | None) -> None: ...

    async def mark_sent(
        self, log_id: int, *, provider_message_id: str, response: str, subject: str
    ) -> None: ...

    async def mark_failed(self, log_id: int, error: str, subject: str | None) -> None: ...

    async def mark_suppressed(self, log_id: int, reason: str, subject: str) -> None: ...


LOG_SQL = text(
    """
    SELECT id, municipality_id, template, to_email, order_id, user_id, status, attempts
    FROM email_log WHERE id = :id AND municipality_id = :m
    """
)
ORDER_SQL = text(
    """
    SELECT o.id, o.reference, o.email, o.first_name, o.parcel_label, o.document_name,
           o.price_eur, o.currency, o.turnaround_business_days, o.expected_by,
           f.object_key AS report_key
    FROM orders o LEFT JOIN stored_files f ON f.id = o.report_file_id
    WHERE o.id = :id AND o.municipality_id = :m
    """
)
STAFF_USER_SQL = text(
    "SELECT id, email, display_name, role, is_active FROM staff_users "
    "WHERE id = :id AND municipality_id = :m"
)
INSERT_TOKEN_SQL = text(
    """
    INSERT INTO staff_login_tokens (user_id, token_hash, expires_at, email_log_id)
    VALUES (:user_id, :token_hash, :expires_at, :email_log_id) RETURNING id
    """
)
ATTEMPT_SQL = text(
    """
    UPDATE email_log SET attempts = attempts + 1, error = :error,
        subject = COALESCE(:subject, subject), updated_at = now()
    WHERE id = :id
    """
)
SENT_SQL = text(
    """
    UPDATE email_log SET status = 'sent', attempts = attempts + 1, error = NULL,
        provider_message_id = :provider_message_id, provider_response = :response,
        subject = :subject, sent_at = now(), updated_at = now()
    WHERE id = :id
    """
)
FAILED_SQL = text(
    """
    UPDATE email_log SET status = 'failed', attempts = attempts + 1, error = :error,
        subject = COALESCE(:subject, subject), updated_at = now()
    WHERE id = :id
    """
)
SUPPRESSED_SQL = text(
    """
    UPDATE email_log SET status = 'suppressed', suppressed_reason = :reason, subject = :subject,
        updated_at = now()
    WHERE id = :id
    """
)


class SqlEmailRepository:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], municipality_id: str
    ) -> None:
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def _one(self, statement: Any, params: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            return (await session.execute(statement, params)).mappings().first()

    async def _write(self, statement: Any, params: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            result = await session.execute(statement, params)
            value = result.scalar_one_or_none() if result.returns_rows else None
            await session.commit()
            return value

    async def load_log(self, log_id: int) -> EmailLogRow | None:
        row = await self._one(LOG_SQL, {"id": log_id, "m": self.municipality_id})
        return EmailLogRow(**{k: row[k] for k in EmailLogRow.__slots__}) if row else None

    async def load_order(self, order_id: int) -> OrderMailFacts | None:
        row = await self._one(ORDER_SQL, {"id": order_id, "m": self.municipality_id})
        if row is None:
            return None
        return OrderMailFacts(
            id=int(row["id"]),
            reference=row["reference"],
            email=row["email"],
            first_name=row["first_name"],
            parcel_label=row["parcel_label"],
            document_name=row["document_name"],
            price_eur=float(row["price_eur"]),
            currency=row["currency"],
            turnaround_business_days=int(row["turnaround_business_days"]),
            expected_by=row["expected_by"],
            report_key=row["report_key"],
        )

    async def load_staff_user(self, user_id: int) -> StaffUserFacts | None:
        row = await self._one(STAFF_USER_SQL, {"id": user_id, "m": self.municipality_id})
        if row is None:
            return None
        return StaffUserFacts(
            id=int(row["id"]),
            email=row["email"],
            display_name=row["display_name"],
            role=row["role"],
            is_active=bool(row["is_active"]),
        )

    async def create_login_token(
        self, user_id: int, token_hash: str, expires_at: datetime, email_log_id: int
    ) -> int:
        return int(
            await self._write(
                INSERT_TOKEN_SQL,
                {
                    "user_id": user_id,
                    "token_hash": token_hash,
                    "expires_at": expires_at,
                    "email_log_id": email_log_id,
                },
            )
        )

    async def mark_attempt(self, log_id: int, error: str, subject: str | None) -> None:
        await self._write(ATTEMPT_SQL, {"id": log_id, "error": error, "subject": subject})

    async def mark_sent(
        self, log_id: int, *, provider_message_id: str, response: str, subject: str
    ) -> None:
        await self._write(
            SENT_SQL,
            {
                "id": log_id,
                "provider_message_id": provider_message_id,
                "response": response,
                "subject": subject,
            },
        )

    async def mark_failed(self, log_id: int, error: str, subject: str | None) -> None:
        await self._write(FAILED_SQL, {"id": log_id, "error": error, "subject": subject})

    async def mark_suppressed(self, log_id: int, reason: str, subject: str) -> None:
        await self._write(SUPPRESSED_SQL, {"id": log_id, "reason": reason, "subject": subject})


@dataclass(slots=True)
class MemoryEmailRepository:
    """Unit-test double with the same transitions; ``logs`` rows are plain dicts."""

    logs: dict[int, dict[str, Any]] = field(default_factory=dict)
    orders: dict[int, OrderMailFacts] = field(default_factory=dict)
    users: dict[int, StaffUserFacts] = field(default_factory=dict)
    tokens: list[dict[str, Any]] = field(default_factory=list)

    def add_log(
        self,
        template: str,
        to_email: str,
        *,
        order_id: int | None = None,
        user_id: int | None = None,
        municipality_id: str = "podgorica",
    ) -> int:
        log_id = len(self.logs) + 1
        self.logs[log_id] = {
            "id": log_id,
            "municipality_id": municipality_id,
            "template": template,
            "to_email": to_email,
            "order_id": order_id,
            "user_id": user_id,
            "status": "queued",
            "attempts": 0,
            "error": None,
            "subject": None,
            "provider_message_id": None,
            "provider_response": None,
            "suppressed_reason": None,
        }
        return log_id

    async def load_log(self, log_id: int) -> EmailLogRow | None:
        row = self.logs.get(log_id)
        return EmailLogRow(**{k: row[k] for k in EmailLogRow.__slots__}) if row else None

    async def load_order(self, order_id: int) -> OrderMailFacts | None:
        return self.orders.get(order_id)

    async def load_staff_user(self, user_id: int) -> StaffUserFacts | None:
        return self.users.get(user_id)

    async def create_login_token(
        self, user_id: int, token_hash: str, expires_at: datetime, email_log_id: int
    ) -> int:
        self.tokens.append(
            {
                "id": len(self.tokens) + 1,
                "user_id": user_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
                "email_log_id": email_log_id,
                "created_at": datetime.now(UTC),
            }
        )
        return len(self.tokens)

    async def mark_attempt(self, log_id: int, error: str, subject: str | None) -> None:
        row = self.logs[log_id]
        row["attempts"] += 1
        row["error"] = error
        row["subject"] = subject or row["subject"]

    async def mark_sent(
        self, log_id: int, *, provider_message_id: str, response: str, subject: str
    ) -> None:
        self.logs[log_id].update(
            status="sent",
            attempts=self.logs[log_id]["attempts"] + 1,
            error=None,
            provider_message_id=provider_message_id,
            provider_response=response,
            subject=subject,
        )

    async def mark_failed(self, log_id: int, error: str, subject: str | None) -> None:
        row = self.logs[log_id]
        row.update(status="failed", attempts=row["attempts"] + 1, error=error)
        row["subject"] = subject or row["subject"]

    async def mark_suppressed(self, log_id: int, reason: str, subject: str) -> None:
        self.logs[log_id].update(status="suppressed", suppressed_reason=reason, subject=subject)
