"""The cadastral base (migration 0024, ``core.cadastre``).

- ``cadastral_datasets``: one row per import of a cadastral export (``dataset_version``) with its
  provenance (source, method, retrieval date, licence note, the basis of the access, file checksum,
  source CRS and the transformation used), the validation report and the diff against the previous
  version. The parcels go to ``staging_geometry`` as a ``cadastral_parcels`` batch and the KO
  boundaries as a ``cadastral_municipalities`` batch; nothing is served from here. The publish job
  upserts them (stable ids by KO + number + sub-number) and retires the parcels of the dataset's
  KOs that the new export no longer contains. Batches and dataset rows are never deleted: every
  version stays as history.
- ``cadastral_municipalities``: the served KO table (name, code, boundary, parcel count) behind the
  KO + number lookup and the search dropdown.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.models.planning import gist_index, multipolygon


class CadastralDataset(Base):
    __tablename__ = "cadastral_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'staged'"),
        comment="staged | invalid | published | superseded",
    )
    source_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="adapter: uzn_geoportal | emapa (core.cadastre.adapters)"
    )
    source_name: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text, nullable=False, comment="file | wfs")
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="when the export was obtained"
    )
    access_basis: Mapped[str | None] = mapped_column(
        Text, comment="the agreement or written permission that confirms bulk access"
    )
    licence_note: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    file_key: Mapped[str | None] = mapped_column(
        Text, comment="copy of the export in the private bucket, when stored"
    )
    source_crs: Mapped[str | None] = mapped_column(
        Text, comment="the export's coordinate reference system (kept as metadata)"
    )
    transform: Mapped[str | None] = mapped_column(
        Text, comment="coordinate operation used to reach EPSG:4326 (null = PROJ default)"
    )
    parcels_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("geometry_batches.id", ondelete="SET NULL"),
        comment="the staging_geometry batch (layer cadastral_parcels)",
    )
    ko_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("geometry_batches.id", ondelete="SET NULL"),
        comment="the staging_geometry batch (layer cadastral_municipalities)",
    )
    previous_dataset_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cadastral_datasets.id", ondelete="SET NULL"),
        comment="the version the diff compares with",
    )
    parcel_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ko_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ownership: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="whether the ownership / legal-burden flags were loaded, and from what",
    )
    validation: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="the validation report (a dataset with errors is never staged)"
    )
    diff: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="added / removed / changed parcels against the previous version"
    )
    imported_by: Mapped[str | None] = mapped_column(Text)
    published_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "municipality_id", "dataset_version", name="uq_cadastral_datasets_version"
        ),
        CheckConstraint(
            "status IN ('staged', 'invalid', 'published', 'superseded')",
            name="ck_cadastral_datasets_status",
        ),
        CheckConstraint("method IN ('file', 'wfs')", name="ck_cadastral_datasets_method"),
        Index("ix_cadastral_datasets_municipality_status", "municipality_id", "status"),
    )


class CadastralMunicipality(Base):
    """A cadastral municipality (KO): the name people search by, its code and its boundary."""

    __tablename__ = "cadastral_municipalities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    ko_name: Mapped[str] = mapped_column(Text, nullable=False)
    ko_code: Mapped[str | None] = mapped_column(Text)
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    parcel_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    boundary_source: Mapped[str] = mapped_column(
        Text, nullable=False, comment="delivered | derived_from_parcels"
    )
    dataset_version: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        gist_index("cadastral_municipalities", "geom"),
        Index(
            "uq_cadastral_municipalities_name",
            "municipality_id",
            text("lower(ko_name)"),
            unique=True,
        ),
        CheckConstraint(
            "boundary_source IN ('delivered', 'derived_from_parcels')",
            name="ck_cadastral_municipalities_boundary_source",
        ),
    )
