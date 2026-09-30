"""Pre-processing a stored planning PDF (the extraction and geometry jobs' first stage), cached
by checksum.

1. The file record (``stored_files``) must be a planning-document PDF.
2. Unless its manifest is current (same checksum, ``PREPROCESS_VERSION`` and options): read the
   PDF from the private bucket (the checksum is verified), ``extract_pages`` (scanned pages stay
   unread and are listed, never OCRed), sections and the chunk plan; store the page data as gzip
   JSON next to the upload and the manifest on the file record (``stored_files.preprocess``).

A re-run of an unchanged file reuses its manifest; ``force`` redoes the analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.extraction.chunking import ChunkPlan, SectionRules, detect_sections, plan_chunks
from core.extraction.manifest import (
    PreprocessManifest,
    build_manifest,
    dump_pages,
    is_current,
    load_pages,
)
from core.extraction.preprocess import (
    PREPROCESS_VERSION,
    DocumentPages,
    PreprocessOptions,
    extract_pages,
)

FILE_SQL = text(
    "SELECT id, kind, object_key, sha256, page_count, preprocess FROM stored_files "
    "WHERE municipality_id = :m AND id = :id"
)
SAVE_SQL = text(
    "UPDATE stored_files SET preprocess = CAST(:manifest AS jsonb), "
    "page_count = COALESCE(page_count, :pages) WHERE municipality_id = :m AND id = :id"
)


class PreprocessError(Exception):
    """The file cannot be pre-processed (unknown, not a planning PDF, content changed)."""


def page_data_key(object_key: str) -> str:
    """The page data object next to the uploaded PDF."""
    return f"{object_key.rsplit('/', 1)[0]}/preprocess-v{PREPROCESS_VERSION}.json.gz"


def read_manifest(raw: Any) -> PreprocessManifest | None:
    """The stored manifest, or None when there is none or it is of another shape."""
    if not raw:
        return None
    try:
        return PreprocessManifest.model_validate(raw)
    except ValidationError:
        return None


def load_document_pages(storage: Any, manifest: PreprocessManifest) -> DocumentPages:
    """The full page data a manifest points at (for the extraction job)."""
    return load_pages(storage.get_bytes(manifest.page_data_key))


class PreprocessRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: Any,
        *,
        municipality_id: str,
        options: PreprocessOptions,
        rules: SectionRules | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.municipality_id = municipality_id
        self.options = options
        self.rules = rules or SectionRules.from_profile(municipality_id)
        self.clock = clock

    def _analyse(self, pdf: bytes) -> tuple[DocumentPages, list[ChunkPlan]]:
        doc = extract_pages(pdf, options=self.options)
        detect_sections(doc, self.rules)
        return doc, plan_chunks(doc, self.options)

    async def run(self, file_id: int, *, force: bool = False) -> dict[str, Any]:
        params = {"m": self.municipality_id, "id": file_id}
        async with self.session_factory() as session:
            row = (await session.execute(FILE_SQL, params)).mappings().first()
        if row is None:
            raise PreprocessError(f"no stored file with id {file_id}")
        if row["kind"] != "planning_document":
            raise PreprocessError(f"file {file_id} is a {row['kind']}, not a planning PDF")

        manifest = read_manifest(row["preprocess"])
        cached = not force and is_current(manifest, row["sha256"], self.options)
        if not cached or manifest is None:
            pdf = await asyncio.to_thread(self.storage.get_bytes, row["object_key"])
            if hashlib.sha256(pdf).hexdigest() != row["sha256"]:
                raise PreprocessError(f"the stored object of file {file_id} changed")
            doc, chunks = await asyncio.to_thread(self._analyse, pdf)
            key = page_data_key(row["object_key"])
            await asyncio.to_thread(
                self.storage.put_bytes, key, dump_pages(doc), "application/gzip"
            )
            manifest = build_manifest(
                doc, chunks, options=self.options, page_data_key=key, created_at=self.clock()
            )
            async with self.session_factory() as session:
                await session.execute(
                    SAVE_SQL,
                    {
                        **params,
                        "manifest": manifest.model_dump_json(),
                        "pages": len(manifest.pages),
                    },
                )
                await session.commit()
        return {
            "file_id": file_id,
            "sha256": row["sha256"],
            "cached": cached,
            "summary": manifest.summary.model_dump(mode="json"),
        }
