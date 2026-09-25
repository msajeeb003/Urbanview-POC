"""Source viewer: every planning value traceable to its document page in one click.

``GET /v1/source/{document_id}/page/{page}`` and ``GET /v1/source/value/{value_id}`` answer with
one short-lived signed URL into the private bucket: the rendered page image when the ingestion
job produced one, otherwise the PDF itself with a ``#page=N`` anchor. Nothing else leaves the
API: object keys stay internal, the bucket stays private and the URL dies after
``SOURCE_URL_EXPIRES_SECONDS``.

What exists is decided by the database, never by probing storage: ``planning_documents.file_key``
(null = not stored), ``page_count`` (null = unknown, so any page >= 1 of the PDF is served) and
``page_images_rendered`` (page images only for a page within a known page count). A value is
looked up in the serving table only; its page is the page it cites. 404 ``not_found`` is for a
document, value or page that truly does not exist; storage trouble is 503
``service_unavailable``. The client emits ``source_reference_opened`` itself: ``document_id`` and
``page`` are in every response for that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from api.schemas.source import SourcePage, SourceValue
from core.errors import NotFoundError, ServiceUnavailableError
from core.storage import ObjectStorage

log = logging.getLogger("urbanview.source")


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: int
    name: str
    status: str
    registry_url: str | None
    file_key: str | None
    page_count: int | None
    page_images_rendered: bool


@dataclass(frozen=True, slots=True)
class ValueRow:
    id: int
    field_key: str
    label_en: str
    label_me: str
    value_text: str | None
    value_number: float | None
    unit: str | None
    urban_parcel_id: int | None
    source_page: int
    source_bbox: list[float] | None
    source_note: str | None
    document: DocumentRow


class SourceRepository(Protocol):
    async def get_document(self, document_id: int) -> DocumentRow | None: ...

    async def get_value(self, value_id: int) -> ValueRow | None: ...


class StorageSigner(Protocol):
    def presigned_get_url(
        self,
        key: str,
        expires_in: int = 900,
        *,
        content_type: str | None = None,
        inline: bool = False,
    ) -> str: ...


# --- SQL repository (serving tables only, one statement per call) -------------------------------

DOCUMENT_SQL = text(
    """
    SELECT d.id, d.name, d.status::text AS status, d.source_url AS registry_url,
           d.file_key, d.page_count, d.page_images_rendered
    FROM planning_documents d
    WHERE d.id = :document_id AND d.municipality_id = :municipality_id
    """
)

VALUE_SQL = text(
    """
    SELECT v.id AS value_id, v.field_key, f.label_en, f.label_me, v.value_text, v.value_number,
           COALESCE(v.unit, f.unit) AS unit, v.urban_parcel_id, v.source_page, v.source_bbox,
           v.source_note,
           d.id, d.name, d.status::text AS status, d.source_url AS registry_url,
           d.file_key, d.page_count, d.page_images_rendered
    FROM planning_parameter_values v
    JOIN planning_documents d ON d.id = v.document_id
    JOIN planning_fields f ON f.key = v.field_key
    WHERE v.id = :value_id AND v.municipality_id = :municipality_id
    """
)


def _document(row: Mapping[str, Any]) -> DocumentRow:
    return DocumentRow(
        id=int(row["id"]),
        name=row["name"],
        status=row["status"],
        registry_url=row.get("registry_url"),
        file_key=row.get("file_key"),
        page_count=row.get("page_count"),
        page_images_rendered=bool(row.get("page_images_rendered")),
    )


class SqlSourceRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], municipality_id: str):
        self.session_factory = session_factory
        self.municipality_id = municipality_id

    async def get_document(self, document_id: int) -> DocumentRow | None:
        params = {"document_id": document_id, "municipality_id": self.municipality_id}
        async with self.session_factory() as session:
            row = (await session.execute(DOCUMENT_SQL, params)).mappings().first()
        return _document(row) if row is not None else None

    async def get_value(self, value_id: int) -> ValueRow | None:
        params = {"value_id": value_id, "municipality_id": self.municipality_id}
        async with self.session_factory() as session:
            row = (await session.execute(VALUE_SQL, params)).mappings().first()
        if row is None:
            return None
        return ValueRow(
            id=int(row["value_id"]),
            field_key=row["field_key"],
            label_en=row["label_en"],
            label_me=row["label_me"],
            value_text=row.get("value_text"),
            value_number=row.get("value_number"),
            unit=row.get("unit"),
            urban_parcel_id=row.get("urban_parcel_id"),
            source_page=int(row["source_page"]),
            source_bbox=row.get("source_bbox"),
            source_note=row.get("source_note"),
            document=_document(row),
        )


# --- page links (shared with the review queue) --------------------------------------------------


def signed_page_link(
    storage: StorageSigner,
    municipality_id: str,
    *,
    document_id: int,
    file_key: str | None,
    page_count: int | None,
    page_images_rendered: bool,
    page: int | None,
    expires_in_seconds: int,
    now: datetime,
) -> dict[str, Any] | None:
    """The same decision as the source viewer, as data: the page image when rendered within a
    known page count, else the PDF with a ``#page=N`` anchor; ``None`` when nothing is stored or
    the page is unknown / beyond the document."""
    if not file_key or page is None or page < 1:
        return None
    if page_count is not None and page > page_count:
        return None
    if page_images_rendered and page_count is not None:
        key = ObjectStorage.page_image_key(municipality_id, document_id, page)
        kind, content_type, fragment = "page_image", "image/png", ""
    else:
        key = file_key
        kind, content_type, fragment = "pdf_page", "application/pdf", f"#page={page}"
    url = storage.presigned_get_url(key, expires_in_seconds, content_type=content_type, inline=True)
    return {
        "url": url + fragment,
        "kind": kind,
        "content_type": content_type,
        "expires_at": now + timedelta(seconds=expires_in_seconds),
    }


# --- service -----------------------------------------------------------------------------------


class SourceService:
    def __init__(
        self,
        repository: SourceRepository,
        storage: StorageSigner,
        *,
        municipality_id: str,
        expires_in_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.municipality_id = municipality_id
        self.expires_in_seconds = int(expires_in_seconds)
        self.clock = clock

    async def page(self, document_id: int, page: int) -> SourcePage:
        document = await self.repository.get_document(document_id)
        if document is None:
            raise NotFoundError(
                f"No planning document with id {document_id}", details={"document_id": document_id}
            )
        return await self._page_payload(document, page, value=None)

    async def value(self, value_id: int) -> SourcePage:
        value = await self.repository.get_value(value_id)
        if value is None:
            raise NotFoundError(
                f"No published planning value with id {value_id}", details={"value_id": value_id}
            )
        block = SourceValue(
            value_id=value.id,
            field_key=value.field_key,
            label_en=value.label_en,
            label_me=value.label_me,
            value=value.value_number if value.value_number is not None else value.value_text,
            unit=value.unit,
            urban_parcel_id=value.urban_parcel_id,
            bbox=value.source_bbox,
            note=value.source_note,
        )
        return await self._page_payload(value.document, value.source_page, value=block)

    async def _page_payload(
        self, document: DocumentRow, page: int, *, value: SourceValue | None
    ) -> SourcePage:
        if document.file_key is None:
            raise NotFoundError(
                "The document file is not stored yet; there is no page to show",
                details={"document_id": document.id, "page": page, "reason": "not_stored"},
            )
        if document.page_count is not None and page > document.page_count:
            raise NotFoundError(
                f"Page {page} does not exist: the document has {document.page_count} pages",
                details={
                    "document_id": document.id,
                    "page": page,
                    "page_count": document.page_count,
                },
            )
        # Page images are addressed by convention; they are only trusted within a known page count.
        if document.page_images_rendered and document.page_count is not None:
            key = ObjectStorage.page_image_key(self.municipality_id, document.id, page)
            kind, content_type, fragment = "page_image", "image/png", ""
        else:
            key = document.file_key
            kind, content_type, fragment = "pdf_page", "application/pdf", f"#page={page}"

        issued_at = self.clock()
        try:
            url = await run_in_threadpool(
                self.storage.presigned_get_url,
                key,
                self.expires_in_seconds,
                content_type=content_type,
                inline=True,
            )
        except (BotoCoreError, ClientError) as exc:
            log.warning(
                "could not sign a source URL (%s: %s)",
                type(exc).__name__,
                exc,
                extra={"document_id": document.id, "page": page},
            )
            raise ServiceUnavailableError("Object storage is not available") from exc

        return SourcePage(
            document_id=document.id,
            document_name=document.name,
            document_status=document.status,  # type: ignore[arg-type]
            page=page,
            page_count=document.page_count,
            kind=kind,  # type: ignore[arg-type]
            url=url + fragment,
            content_type=content_type,  # type: ignore[arg-type]
            expires_at=issued_at + timedelta(seconds=self.expires_in_seconds),
            expires_in_seconds=self.expires_in_seconds,
            registry_url=document.registry_url,
            value=value,
        )
