"""Municipality profiles: the configuration layer that keeps the core municipality-agnostic.

Everything specific to a place (bounds, CRS, cadastral municipalities, planning terminology, data
sources) is declared in ``municipalities/<id>.toml`` and loaded here. Code in ``core``/``api``/
``jobs`` reads these values through the profile and must never hard-code them, so adding a
municipality is a data + configuration exercise (BRD §8).
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROFILES_DIR = Path(__file__).resolve().parent.parent / "municipalities"


class TermLabel(BaseModel):
    abbreviation: str
    local_name: str
    english: str


class Terminology(BaseModel):
    site_coverage: TermLabel
    far: TermLabel
    cadastral_municipality: TermLabel
    urban_parcel: TermLabel
    urban_block: TermLabel
    document_types: dict[str, str] = Field(default_factory=dict)
    # English display names shown after the abbreviation ("DUP — Detailed urban plan")
    document_types_en: dict[str, str] = Field(default_factory=dict)


class DataSource(BaseModel):
    id: str
    kind: Literal["planning", "cadastre", "market", "reference"]
    name: str
    url: str
    provides: str


class CrsCandidate(BaseModel):
    """A coordinate system plans are drawn in, with the value ranges its labels fall in."""

    epsg: int
    name: str
    easting: tuple[float, float]
    northing: tuple[float, float]


class GisProfile(BaseModel):
    """What the GIS track needs to know about this place's plans (``core.gis``).

    Patterns are case-insensitive regular expressions matched against accent-folded text
    (``core.gis.inspect_pdf.fold``): layer names without their xref prefix, title-block text.
    """

    crs_candidates: list[CrsCandidate] = Field(default_factory=list)
    crs_keywords: list[str] = Field(default_factory=list)
    legend_keywords: list[str] = Field(default_factory=lambda: ["legenda", "legend"])
    coordinate_keywords: list[str] = Field(default_factory=lambda: ["koordinat"])
    sheet_titles: dict[str, list[str]] = Field(default_factory=dict)
    # layer type -> patterns, for plan sheets (DUP / UP graphics) and for base maps (PUP atlas)
    plan_layers: dict[str, list[str]] = Field(default_factory=dict)
    base_map_layers: dict[str, list[str]] = Field(default_factory=dict)


class GlossaryEntry(BaseModel):
    term: str
    meaning: str


class FloorNotation(BaseModel):
    """Tokens of the local floor notation (``Po+P+6``), compared case-insensitively. Upper floors
    are plain numbers; a count may prefix a below-ground token (``2Po``)."""

    below_ground: list[str] = Field(default_factory=list)
    ground: list[str] = Field(default_factory=list)
    attic: list[str] = Field(default_factory=list)


class LandUseTerm(BaseModel):
    """A land-use wording pattern (regular expression on accent-folded lower-case text) and the
    UrbanView land-use class it means (``core.extraction.schema.LandUseClass``)."""

    pattern: str
    category: str


class ExtractionProfile(BaseModel):
    """What AI document extraction needs to know about this place's documents (``core.extraction``):
    their language, the planning terms the prompts explain, the floor notation and the land-use
    wording. The prompt templates stay free of place-specific words."""

    language: str
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    floor_notation: FloorNotation = Field(default_factory=FloorNotation)
    land_use_terms: list[LandUseTerm] = Field(default_factory=list)
    # words dropped from the start of a block label to get its key ("Blok A", "UKUPNO BLOK A")
    block_label_words: list[str] = Field(default_factory=list)
    # planning section (urban_parcels, regulation, land_use, infrastructure) -> heading patterns
    sections: dict[str, list[str]] = Field(default_factory=dict)


class MarketHeaders(BaseModel):
    """Patterns (regular expressions on accent-folded lower-case words, Cyrillic transliterated)
    that label the columns of market tables: the area column, the four metrics, the bounds,
    periods, columns to leave alone, and the "in thousands" marker."""

    geography: list[str] = Field(default_factory=list)
    land_rate: list[str] = Field(default_factory=list)
    build_rate: list[str] = Field(default_factory=list)
    design_rate: list[str] = Field(default_factory=list)
    sale_rate: list[str] = Field(default_factory=list)
    low: list[str] = Field(default_factory=list)
    high: list[str] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    period: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    thousands: list[str] = Field(default_factory=list)


class MarketPeriods(BaseModel):
    """Local words of period labels (``IV kvartal 2025``, ``I polugodište``, ``septembar``);
    the English ones are built in (``core.market.parse``)."""

    months: list[str] = Field(default_factory=list, description="January first, 12 names")
    quarter: list[str] = Field(default_factory=list)
    half: list[str] = Field(default_factory=list)


class MarketProfile(BaseModel):
    """What market-data imports need to know about this place (``core.market``): who publishes
    the official statistics, the names that mean the whole municipality, other spellings of the
    zones, the words of table headers and periods, which metrics each import kind may give and
    the plausible range of each metric (outside = flagged for the reviewer, never dropped)."""

    statistics_source: str = "Official statistics"
    municipality_names: list[str] = Field(default_factory=list)
    zone_aliases: dict[str, list[str]] = Field(default_factory=dict)
    kind_metrics: dict[str, list[str]] = Field(default_factory=dict)
    plausible_eur_m2: dict[str, tuple[float, float]] = Field(default_factory=dict)
    headers: MarketHeaders = Field(default_factory=MarketHeaders)
    periods: MarketPeriods = Field(default_factory=MarketPeriods)


class MunicipalityProfile(BaseModel):
    id: str
    name: str
    country: str
    locale: str = "en"
    currency: str = "EUR"
    serving_crs_epsg: int = 4326
    source_crs_epsg: int | None = None
    center: tuple[float, float] = Field(description="lng, lat")
    bounds: tuple[float, float, float, float] = Field(
        description="min_lng, min_lat, max_lng, max_lat"
    )
    cadastral_municipalities: list[str] = Field(default_factory=list)
    price_band_breaks_eur_m2: list[float] = Field(
        default_factory=list,
        description=(
            "Sale-price choropleth band starts in €/m² (0 = not saleable is its own band); "
            "empty = quantile classes"
        ),
    )
    terminology: Terminology
    sources: list[DataSource] = Field(default_factory=list)
    # The profile file's [gis], [extraction] and [market] tables are configuration for core.gis,
    # core.extraction and core.market, read by load_gis_profile / load_extraction_profile /
    # load_market_profile; this model (served by GET /v1/municipality) ignores them.

    def contains(self, lng: float, lat: float) -> bool:
        min_lng, min_lat, max_lng, max_lat = self.bounds
        return min_lng <= lng <= max_lng and min_lat <= lat <= max_lat


class UnknownMunicipalityError(LookupError):
    pass


def list_municipality_ids() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


def _read_profile(municipality_id: str) -> dict:
    path = PROFILES_DIR / f"{municipality_id}.toml"
    if not path.is_file():
        raise UnknownMunicipalityError(
            f"no profile for municipality '{municipality_id}' (looked in {PROFILES_DIR})"
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


@cache
def load_profile(municipality_id: str) -> MunicipalityProfile:
    return MunicipalityProfile.model_validate(_read_profile(municipality_id))


@cache
def load_gis_profile(municipality_id: str) -> GisProfile | None:
    """The profile's [gis] table (core.gis tooling), or None when the municipality has none."""
    table = _read_profile(municipality_id).get("gis")
    return None if table is None else GisProfile.model_validate(table)


@cache
def load_extraction_profile(municipality_id: str) -> ExtractionProfile | None:
    """The profile's [extraction] table (core.extraction), or None when there is none."""
    table = _read_profile(municipality_id).get("extraction")
    return None if table is None else ExtractionProfile.model_validate(table)


@cache
def load_market_profile(municipality_id: str) -> MarketProfile:
    """The profile's [market] table (core.market); an empty profile when there is none (every
    table then needs the LLM step or fails to map)."""
    table = _read_profile(municipality_id).get("market")
    return MarketProfile() if table is None else MarketProfile.model_validate(table)
