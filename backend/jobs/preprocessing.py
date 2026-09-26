"""Pre-processing a stored planning PDF (job ``preprocess_file``), cached by checksum.

1. The file record (``stored_files``) must be a planning-document PDF.
2. Unless its manifest is current (same checksum, ``PREPROCESS_VERSION`` and options): read the
   PDF from the private bucket (the checksum is verified), ``extract_pages`` (OCR only through a
   configured backend), sections and the chunk plan; store the page data as gzip JSON next to the
   upload and the manifest on the file record (``stored_files.preprocess``).
3. Page images (PNG at ``PREPROCESS_PAGE_IMAGE_DPI``, capped at ``..._MAX_PIXELS`` for large
   sheets) for every document version registered on the file that lacks them at that dpi, at the
   source viewer's keys (``ObjectStorage.page_image_key``), listed in the manifest. The source
   viewer serves them only with ``PREPROCESS_SERVE_PAGE_IMAGES`` (then ``page_images_rendered``),
   because the public viewer highlights a cited value on the PDF, not on an image.

A re-run of an unchanged file only renders what is missing; ``force`` redoes everything.
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
    PageImages,
    PreprocessManifest,
    build_manifest,
    dump_pages,
    is_current,
    load_pages,
)
from core.extraction.preprocess import (
    PREPROCESS_VERSION,
    DocumentPages,
    OcrBackend,
    PreprocessOptions,
    extract_pages,
    iter_page_images,
)
from core.storage import ObjectStorage

FILE_SQL = text(
    "SELECT id, kind, object_key, sha256, page_count, preprocess FROM stored_files "
    "WHERE municipality_id = :m AND id = :id"
)
DOCUMENTS_SQL = text(
    "SELECT id FROM planning_documents WHERE municipality_id = :m AND file_id = :id ORDER BY id"
)
SAVE_SQL = text(
    "UPDATE stored_files SET preprocess = CAST(:manifest AS jsonb), "
    "page_count = COALESCE(page_count, :pages) WHERE municipality_id = :m AND id = :id"
)
SERVE_SQL = text(
    "UPDATE planning_documents SET page_images_rendered = true, "
    "page_count = COALESCE(page_count, :pages) WHERE municipality_id = :m AND id = ANY(:ids)"
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
        image_dpi: int,
        image_max_pixels: int,
        serve_images: bool = False,
        ocr: OcrBackend | None = None,
        rules: SectionRules | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.municipality_id = municipality_id
        self.options = options
        self.image_dpi = image_dpi
        self.image_max_pixels = image_max_pixels
        self.serve_images = serve_images
        self.ocr = ocr
        self.rules = rules or SectionRules.from_profile(municipality_id)
        self.clock = clock

    def _analyse(self, pdf: bytes) -> tuple[DocumentPages, list[ChunkPlan]]:
        doc = extract_pages(pdf, options=self.options, ocr=self.ocr)
        detect_sections(doc, self.rules)
        return doc, plan_chunks(doc, self.options)

    def _render(self, pdf: bytes, document_id: int) -> list[str]:
        keys: list[str] = []
        pages = iter_page_images(pdf, dpi=self.image_dpi, max_pixels=self.image_max_pixels)
        for number, png, _ in pages:
            key = ObjectStorage.page_image_key(self.municipality_id, document_id, number)
            self.storage.put_bytes(key, png, "image/png")
            keys.append(key)
        return keys

    async def run(self, file_id: int, *, force: bool = False) -> dict[str, Any]:
        params = {"m": self.municipality_id, "id": file_id}
        async with self.session_factory() as session:
            row = (await session.execute(FILE_SQL, params)).mappings().first()
            documents = [int(r[0]) for r in (await session.execute(DOCUMENTS_SQL, params)).all()]
        if row is None:
            raise PreprocessError(f"no stored file with id {file_id}")
        if row["kind"] != "planning_document":
            raise PreprocessError(f"file {file_id} is a {row['kind']}, not a planning PDF")

        now = self.clock()
        manifest = read_manifest(row["preprocess"])
        cached = not force and is_current(manifest, row["sha256"], self.options)
        pdf: bytes | None = None

        async def content() -> bytes:
            data = await asyncio.to_thread(self.storage.get_bytes, row["object_key"])
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise PreprocessError(f"the stored object of file {file_id} changed")
            return data

        if not cached or manifest is None:
            pdf = await content()
            doc, chunks = await asyncio.to_thread(self._analyse, pdf)
            key = page_data_key(row["object_key"])
            await asyncio.to_thread(
                self.storage.put_bytes, key, dump_pages(doc), "application/gzip"
            )
            keep = manifest.page_images if manifest and manifest.sha256 == doc.sha256 else {}
            manifest = build_manifest(
                doc,
                chunks,
                options=self.options,
                page_data_key=key,
                created_at=now,
                page_images=keep,
            )

        rendered: list[int] = []
        for document_id in documents:
            have = manifest.page_images.get(str(document_id))
            complete = have is not None and len(have.keys) == len(manifest.pages)
            if not force and complete and have is not None and have.dpi == self.image_dpi:
                continue
            pdf = pdf or await content()
            keys = await asyncio.to_thread(self._render, pdf, document_id)
            manifest.page_images[str(document_id)] = PageImages(
                dpi=self.image_dpi, keys=keys, rendered_at=now
            )
            rendered.append(document_id)
        manifest.refresh_summary()

        async with self.session_factory() as session:
            await session.execute(
                SAVE_SQL,
                {**params, "manifest": manifest.model_dump_json(), "pages": len(manifest.pages)},
            )
            served = [d for d in documents if str(d) in manifest.page_images]
            if self.serve_images and served:
                await session.execute(
                    SERVE_SQL,
                    {"m": self.municipality_id, "ids": served, "pages": len(manifest.pages)},
                )
            await session.commit()
        return {
            "file_id": file_id,
            "sha256": row["sha256"],
            "cached": cached,
            "images_rendered_for": rendered,
            "page_images_served": self.serve_images,
            "summary": manifest.summary.model_dump(mode="json"),
        }
