"""The pre-processing manifest of a stored PDF, persisted on ``stored_files.preprocess``.

One per file, keyed by its checksum: a re-run with the same file, pre-processing version and
options reuses it (:func:`is_current`) and only renders page images a document does not have yet.
It lists the pages (size, rotation, text or scanned, script, sections), the tables (columns,
stitching), the scanned / unread pages, the chunk plan in reading priority and the page image
keys per document. The full page data (blocks, words, grids) is a gzip JSON object in the private
bucket next to the upload (``page_data_key``); :func:`dump_pages` / :func:`load_pages`.
``summary`` is what the admin document and file records show.
"""

from __future__ import annotations

import gzip
from collections import Counter
from dataclasses import asdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.extraction.chunking import PLANNING_SECTIONS, ChunkPlan
from core.extraction.preprocess import PREPROCESS_VERSION, DocumentPages, PreprocessOptions


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageSummary(_M):
    number: int
    width: float
    height: float
    rotation: int
    chars: int
    method: str  # text | ocr | none
    scanned: bool
    scanned_reason: str | None = None
    blank: bool = False
    script: str
    blocks: int
    tables: int
    tables_skipped: str | None = None
    glyph_decoded: bool = False
    sections: list[str] = Field(default_factory=list)


class TableSummary(_M):
    id: str
    page: int
    method: str
    rows: int = Field(description="Rows below the header")
    columns: list[str]
    continues: str | None = None
    header_from: str | None = None
    sections: list[str] = Field(default_factory=list)


class PageImages(_M):
    dpi: int
    keys: list[str]
    rendered_at: datetime


class PreprocessSummary(_M):
    """What the admin document and file records show."""

    version: str
    preprocessed_at: datetime
    page_count: int
    vector_pages: int = Field(description="Pages with a text layer or vector drawings")
    scanned_pages: list[int] = Field(description="Raster pages: geometry needs manual redraw")
    ocr_pages: list[int] = Field(default_factory=list)
    unread_pages: list[int] = Field(
        default_factory=list, description="Scanned pages nobody has read yet (no OCR backend)"
    )
    blank_pages: list[int] = Field(default_factory=list)
    tables: int
    chunks: int
    sections: dict[str, list[int]] = Field(default_factory=dict)
    scripts: dict[str, int] = Field(default_factory=dict)
    page_images: list[int] = Field(
        default_factory=list, description="Documents whose page images are rendered"
    )


class PreprocessManifest(_M):
    version: str = PREPROCESS_VERSION
    sha256: str
    options_key: str
    options: dict
    created_at: datetime
    page_data_key: str
    pages: list[PageSummary]
    tables: list[TableSummary]
    chunks: list[ChunkPlan] = Field(description="In reading priority")
    page_images: dict[str, PageImages] = Field(
        default_factory=dict, description="Document id -> rendered page images"
    )
    summary: PreprocessSummary

    def refresh_summary(self) -> None:
        self.summary.page_images = sorted(int(k) for k in self.page_images)


def build_manifest(
    doc: DocumentPages,
    chunks: list[ChunkPlan],
    *,
    options: PreprocessOptions,
    page_data_key: str,
    created_at: datetime,
    page_images: dict[str, PageImages] | None = None,
) -> PreprocessManifest:
    sections: dict[str, list[int]] = {}
    for page in doc.pages:
        for section in page.sections:
            sections.setdefault(section, []).append(page.number)
    ordered = {s: sections[s] for s in PLANNING_SECTIONS if s in sections}
    ordered.update({s: v for s, v in sorted(sections.items()) if s not in ordered})
    manifest = PreprocessManifest(
        sha256=doc.sha256,
        options_key=options.key(),
        options=asdict(options),
        created_at=created_at,
        page_data_key=page_data_key,
        pages=[
            PageSummary(
                number=p.number,
                width=p.width,
                height=p.height,
                rotation=p.rotation,
                chars=p.char_count,
                method=p.method,
                scanned=p.scanned,
                scanned_reason=p.scanned_reason,
                blank=p.blank,
                script=p.script,
                blocks=len(p.blocks),
                tables=len(p.tables),
                tables_skipped=p.tables_skipped,
                glyph_decoded=p.glyph_decoded,
                sections=p.sections,
            )
            for p in doc.pages
        ],
        tables=[
            TableSummary(
                id=t.id,
                page=p.number,
                method=t.method,
                rows=len(t.rows) - t.header_rows,
                columns=t.columns,
                continues=t.continues,
                header_from=t.header_from,
                sections=t.sections,
            )
            for p in doc.pages
            for t in p.tables
        ],
        chunks=chunks,
        page_images=page_images or {},
        summary=PreprocessSummary(
            version=PREPROCESS_VERSION,
            preprocessed_at=created_at,
            page_count=doc.page_count,
            vector_pages=sum(1 for p in doc.pages if not p.scanned and not p.blank),
            scanned_pages=[p.number for p in doc.pages if p.scanned],
            ocr_pages=[p.number for p in doc.pages if p.method == "ocr"],
            unread_pages=[p.number for p in doc.pages if p.scanned and p.method == "none"],
            blank_pages=[p.number for p in doc.pages if p.blank],
            tables=sum(len(p.tables) for p in doc.pages),
            chunks=len(chunks),
            sections=ordered,
            scripts=dict(Counter(p.script for p in doc.pages)),
        ),
    )
    manifest.refresh_summary()
    return manifest


def is_current(
    manifest: PreprocessManifest | None, sha256: str, options: PreprocessOptions
) -> bool:
    """The manifest was made from this content, by this version, with these options."""
    return (
        manifest is not None
        and manifest.sha256 == sha256
        and manifest.version == PREPROCESS_VERSION
        and manifest.options_key == options.key()
    )


def dump_pages(doc: DocumentPages) -> bytes:
    return gzip.compress(doc.model_dump_json().encode("utf-8"), mtime=0)


def load_pages(data: bytes) -> DocumentPages:
    return DocumentPages.model_validate_json(gzip.decompress(data))
