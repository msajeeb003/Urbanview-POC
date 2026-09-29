"""``[cadastre]`` of the municipality profile: the sources, their access status and field maps.

Everything place-specific about the cadastral base is configuration: which source the import
uses, whether bulk access to it is confirmed (and on what basis, under which licence), the
coordinate reference system the export comes in and the transformation to use, the names of the
layers and fields, the official KO names, and the validation thresholds. A new municipality, or a
new export format, is a profile change, not code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.municipality import profile_table

Method = Literal["file", "wfs"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParcelFields(_Model):
    """Where the export keeps each attribute (field names as the export writes them)."""

    ko_code: str | None = None
    ko_name: str | None = None  # at least one of ko_code / ko_name
    parcel_number: str
    fid: str | None = None  # the export's own record id, kept for tracing a record back
    sub_number: str | None = None
    # the number field may carry the sub-number ("1234/5"): split at this separator when the
    # export has no sub-number field of its own
    number_separator: str = "/"
    street_address: list[str] = Field(default_factory=list)  # joined with a space

    @model_validator(mode="after")
    def _ko(self) -> ParcelFields:
        if not (self.ko_code or self.ko_name):
            raise ValueError("fields: map ko_name or ko_code (the KO is mandatory)")
        return self


class KoFields(_Model):
    ko_code: str | None = None
    ko_name: str | None = None

    @model_validator(mode="after")
    def _ko(self) -> KoFields:
        if not (self.ko_code or self.ko_name):
            raise ValueError("ko_fields: map ko_name or ko_code")
        return self


class OwnershipFields(_Model):
    """An attribute export of the flags, keyed like the parcels. A flag is set only from an
    explicit value of the export listed in ``true_values`` / ``false_values``; anything else leaves
    it unknown (null). Nothing is inferred from other attributes."""

    ko_code: str | None = None
    ko_name: str | None = None
    parcel_number: str
    sub_number: str | None = None
    number_separator: str = "/"
    public_ownership: str | None = None
    restitution_or_legal_burden: str | None = None
    true_values: list[str] = Field(default_factory=lambda: ["1", "true", "da", "yes", "y", "t"])
    false_values: list[str] = Field(default_factory=lambda: ["0", "false", "ne", "no", "n", "f"])

    @model_validator(mode="after")
    def _keys(self) -> OwnershipFields:
        if not (self.ko_code or self.ko_name):
            raise ValueError("ownership_fields: map ko_name or ko_code")
        if not (self.public_ownership or self.restitution_or_legal_burden):
            raise ValueError("ownership_fields: map at least one flag")
        return self


class SourceConfig(_Model):
    """One source of the cadastral base and what is known about access to it."""

    id: str
    name: str
    url: str | None = None
    kind: Literal["parcels", "ownership"]
    # "confirmed" only once the source has agreed to provide the data in bulk (P0): an
    # agreement, a written permission or published open-data terms, named in access_basis
    access: Literal["confirmed", "not_confirmed"] = "not_confirmed"
    access_basis: str | None = None
    licence_note: str | None = None
    methods: list[Method] = Field(default_factory=lambda: ["file"])  # what the source offers
    method: Method = "file"  # what the import uses
    source_crs: str | None = None  # assumed when the export carries none (e.g. "EPSG:25834")
    transform: str | None = None  # ogr2ogr -ct: a PROJ pipeline or EPSG operation for datum shifts
    parcel_layer: str | None = None  # layer of the export (empty: the only / first layer)
    ko_layer: str | None = None  # KO boundaries layer, when the export has one
    wfs_url: str | None = None
    wfs_typename: str | None = None
    wfs_ko_typename: str | None = None
    wfs_page_size: int = Field(default=1000, ge=1)
    fields: ParcelFields | None = None
    ko_fields: KoFields | None = None
    ownership_fields: OwnershipFields | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SourceConfig:
        if self.method not in self.methods:
            raise ValueError(f"source {self.id}: method {self.method} is not in methods")
        if self.access == "confirmed" and not (self.access_basis and self.licence_note):
            raise ValueError(
                f"source {self.id}: confirmed access needs access_basis (the agreement or "
                "permission) and licence_note"
            )
        if self.kind == "parcels" and self.fields is None:
            raise ValueError(f"source {self.id}: a parcel source needs [fields]")
        if self.kind == "ownership" and self.ownership_fields is None:
            raise ValueError(f"source {self.id}: an ownership source needs [ownership_fields]")
        if self.method == "wfs" and not (self.wfs_url and self.wfs_typename):
            raise ValueError(f"source {self.id}: method wfs needs wfs_url and wfs_typename")
        return self


class CadastreProfile(_Model):
    source: str  # the parcel source the import uses unless told otherwise
    ownership_source: str | None = None
    area_crs_epsg: int  # area_m2 is computed in this projected CRS (place data: no default)
    dataset_prefix: str = "cad"
    on_duplicate: Literal["error", "merge"] = "error"  # merge = union the parts of one parcel
    # a new version removing more than this share of the previous version's parcels (in the KOs
    # it covers) is refused unless the import is told to accept it
    mass_change_threshold: float = Field(default=0.2, gt=0, le=1)
    coverage_cell_m: float = Field(default=500, gt=0)  # grid over the profile's bounds
    min_coverage: float = Field(default=0.95, ge=0, le=1)
    ko_names: dict[str, str] = Field(default_factory=dict)  # source KO name or code -> official
    sources: dict[str, SourceConfig]

    @model_validator(mode="before")
    @classmethod
    def _source_ids(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            data = dict(data)
            data["sources"] = {
                key: {"id": key, **value} if isinstance(value, dict) else value
                for key, value in data["sources"].items()
            }
        return data

    @model_validator(mode="after")
    def _known(self) -> CadastreProfile:
        if self.source not in self.sources or self.sources[self.source].kind != "parcels":
            raise ValueError(f"cadastre.source {self.source!r} is not a parcel source")
        if self.ownership_source is not None and (
            self.ownership_source not in self.sources
            or self.sources[self.ownership_source].kind != "ownership"
        ):
            raise ValueError(f"cadastre.ownership_source {self.ownership_source!r} is unknown")
        return self


def load_cadastre_profile(municipality_id: str) -> CadastreProfile:
    table = profile_table(municipality_id, "cadastre")
    if table is None:
        raise LookupError(f"municipalities/{municipality_id}.toml has no [cadastre] table")
    return CadastreProfile.model_validate(table)
