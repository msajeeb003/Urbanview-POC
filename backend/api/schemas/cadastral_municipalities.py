"""`GET /v1/cadastral-municipalities`: the KOs people search parcels by."""

from __future__ import annotations

from pydantic import BaseModel, Field

from api.schemas.locate import LatLng


class CadastralMunicipalityEntry(BaseModel):
    ko_name: str = Field(description="The KO name the parcel lookup takes (?ko=)")
    ko_code: str | None = Field(default=None, description="The cadastre's KO code, when known")
    parcel_count: int = Field(description="Cadastral parcels served in the KO")
    bbox: tuple[float, float, float, float] = Field(
        description="min_lng, min_lat, max_lng, max_lat of the KO boundary"
    )
    centroid: LatLng = Field(description="A point on the KO's surface (label / pin)")
    boundary_source: str = Field(description="delivered | derived_from_parcels")


class CadastralMunicipalities(BaseModel):
    items: list[CadastralMunicipalityEntry]
    dataset_version: str | None = Field(
        default=None, description="The newest cadastral dataset among the KOs"
    )
