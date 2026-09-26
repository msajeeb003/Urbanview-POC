"""The zone dataset's vocabulary and attribute schema: product-wide, never municipality data.

A zone is UrbanView's own internal city division (roughly a city quarter; zones are not official
areas in Montenegro) grouping the planning documents that apply to it. The QGIS working file has
two layers with these attributes:

- ``zones`` (polygons, drawn in the municipality's editing CRS): :data:`ZONE_FIELDS`;
- ``zone_documents`` (a table): :data:`DOCUMENT_FIELDS`, one row per planning document, each in
  exactly one zone. The review aids (how the reference listed it, what eRegistri says, whether the
  client confirmed the row) help the session and are carried through, never required.

Zone types use the map palette's five classes (``frontend/src/styles/wireframe.css`` ``--z-*``,
``frontend/src/lib/layers.ts`` ``ZONE_TYPES``); the QGIS layer stores the long value, the database
the short code (``zones.zone_type``, CHECK in migration 0014).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from core.extraction.textmatch import words_fold


@dataclass(frozen=True, slots=True)
class ZoneType:
    value: str  # the QGIS attribute value
    code: str  # zones.zone_type
    label_en: str
    label_me: str  # provisional until the client confirms the wording
    colour: str  # the map palette (hex)


ZONE_TYPES: tuple[ZoneType, ...] = (
    ZoneType("residential", "res", "Residential", "Stanovanje", "#B5744A"),
    ZoneType("commercial", "com", "Commercial", "Poslovanje", "#BE9A44"),
    ZoneType("mixed", "mix", "Mixed use", "Mješovita namjena", "#8A7A8E"),
    ZoneType(
        "public_institutional", "pub", "Public / institutional", "Javne institucije", "#5E8A82"
    ),
    ZoneType("green_recreation", "grn", "Green / recreation", "Zelenilo i rekreacija", "#7C8A4F"),
)
ZONE_TYPE_BY_VALUE = {t.value: t for t in ZONE_TYPES}
ZONE_TYPE_BY_CODE = {t.code: t for t in ZONE_TYPES}
UNCLASSIFIED_COLOUR = "#9A9A9A"  # a zone without a type is drawn neutral, never a guessed colour

DOCUMENT_STATUSES: tuple[str, ...] = ("adopted", "in_progress", "superseded")
STATUS_LABELS = {
    "adopted": "Adopted (in force)",
    "in_progress": "In progress (not adopted)",
    "superseded": "Superseded / no longer valid",
}

FieldType = Literal["text", "integer", "boolean", "date"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: FieldType
    alias: str
    required: bool = False
    review_aid: bool = False  # helps the session, never needed by the import
    description: str = ""


ZONE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "zone_id",
        "text",
        "Zone ID",
        required=True,
        description="stable slug (lower-case letters, digits, hyphens); never reused",
    ),
    FieldSpec("name", "text", "Name", required=True, description="as the map shows it"),
    FieldSpec(
        "zone_type",
        "text",
        "Zone type",
        description="residential | commercial | mixed | public_institutional | green_recreation",
    ),
    FieldSpec(
        "general_planning_summary",
        "text",
        "General planning summary",
        description="one or two sentences for the zone panel",
    ),
    FieldSpec("notes", "text", "Notes"),
    FieldSpec(
        "no_adopted_plan",
        "boolean",
        "No adopted plan",
        description="true: the zone knowingly has no adopted document (renders as not covered)",
    ),
)

DOCUMENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("zone_id", "text", "Zone", required=True),
    FieldSpec("document_name", "text", "Document name", required=True),
    FieldSpec(
        "document_type",
        "text",
        "Type",
        required=True,
        description="a document type key of the municipality profile (DUP, PUP, PGR, UP ...)",
    ),
    FieldSpec(
        "status", "text", "Status", required=True, description="adopted | in_progress | superseded"
    ),
    FieldSpec(
        "eregistri_reference",
        "text",
        "eRegistri id",
        description="the registry's document id (lamp.gov.me/PlanningDocument/Details/<id>)",
    ),
    FieldSpec("source_url", "text", "Source URL"),
    FieldSpec("adoption_date", "date", "Adoption date", description="YYYY-MM-DD, when known"),
    FieldSpec("notes", "text", "Notes"),
    FieldSpec(
        "poc_coverage",
        "boolean",
        "POC coverage",
        description="one of the POC documents with full parameter data",
    ),
    FieldSpec("confirmed", "boolean", "Confirmed", description="checked with the client"),
    FieldSpec("listed_as", "text", "Listed as", review_aid=True, description="reference listing"),
    FieldSpec("listed_year", "integer", "Listed year", review_aid=True),
    FieldSpec("eregistri_name", "text", "eRegistri name", review_aid=True),
    FieldSpec("eregistri_code", "text", "eRegistri code", review_aid=True),
    FieldSpec("eregistri_gazette", "text", "Gazette", review_aid=True),
    FieldSpec("eregistri_note", "text", "eRegistri note", review_aid=True),
    FieldSpec(
        "match",
        "text",
        "Registry match",
        review_aid=True,
        description="how the row was matched to eRegistri: exact | fuzzy:<score> | none",
    ),
)
ZONE_FIELD_NAMES = tuple(f.name for f in ZONE_FIELDS)
DOCUMENT_FIELD_NAMES = tuple(f.name for f in DOCUMENT_FIELDS)

ZONES_LAYER = "zones"
DOCUMENTS_LAYER = "zone_documents"

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_EXTRA = {"đ": "dj", "Đ": "dj", "ß": "ss", "ø": "o", "æ": "ae", "ł": "l"}


def slugify(name: str) -> str:
    """A stable zone id from a name: "Dajbabe – Ćemovsko polje" -> "dajbabe-cemovsko-polje"."""
    text = "".join(_SLUG_EXTRA.get(c, c) for c in name)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def parse_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "da", "x"}:
        return True
    if text in {"0", "false", "f", "no", "n", "ne"}:
        return False
    raise ValueError(f"not a yes / no value: {value!r}")


_YEAR_SUFFIX = re.compile(r"\s*\((\d{4})\)\s*$")


def split_year(listing: str) -> tuple[str, int | None]:
    """ "DUP Momišići C (2009)" -> ("DUP Momišići C", 2009)."""
    match = _YEAR_SUFFIX.search(listing)
    if not match:
        return listing.strip(), None
    return listing[: match.start()].strip(), int(match.group(1))


def name_key(name: str) -> str:
    """Folded name for matching: accents, case, dashes and punctuation do not count."""
    return words_fold(re.sub(r"[‐-―\-,.;:\"'„“”()]+", " ", name))


def document_identity(row: dict[str, object]) -> tuple[str, str]:
    """The key under which a document may appear only once: its eRegistri id, else its folded
    name plus the listed year (two amendments of one plan share a name)."""
    ref = str(row.get("eregistri_reference") or "").strip()
    if ref:
        return ("eregistri", ref.lower())
    year = row.get("listed_year") or ""
    return ("name", f"{name_key(str(row.get('document_name') or ''))}|{year}")
