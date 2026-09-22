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


class DataSource(BaseModel):
    id: str
    kind: Literal["planning", "cadastre", "market", "reference"]
    name: str
    url: str
    provides: str


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
    terminology: Terminology
    sources: list[DataSource] = Field(default_factory=list)

    def contains(self, lng: float, lat: float) -> bool:
        min_lng, min_lat, max_lng, max_lat = self.bounds
        return min_lng <= lng <= max_lng and min_lat <= lat <= max_lat


class UnknownMunicipalityError(LookupError):
    pass


def list_municipality_ids() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


@cache
def load_profile(municipality_id: str) -> MunicipalityProfile:
    path = PROFILES_DIR / f"{municipality_id}.toml"
    if not path.is_file():
        raise UnknownMunicipalityError(
            f"no profile for municipality '{municipality_id}' (looked in {PROFILES_DIR})"
        )
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return MunicipalityProfile.model_validate(data)
