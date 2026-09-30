"""The heatmap surfaces of a publish version (migration 0027, ``core.choropleth``).

``choropleth_cells``: one row per layer and cell with a value (coverage | far | height | gfa per
urban block from the published planning values, sale_price per zone from the assumptions version
that applies), its legend band, unit and label; a block or zone without a value has no row.
``choropleth_classes``: the classes each layer's bands and the map legend use, stored with the cells
so the legend the API serves is what the tiles were built with.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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

_LAYER_CHECK = "layer IN ('coverage', 'far', 'height', 'gfa', 'sale_price')"


class ChoroplethCell(Base):
    __tablename__ = "choropleth_cells"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    layer: Mapped[str] = mapped_column(
        Text, nullable=False, comment="coverage | far | height | gfa | sale_price"
    )
    cell_type: Mapped[str] = mapped_column(Text, nullable=False, comment="block | zone")
    cell_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="urban_blocks.id | zones.id"
    )
    cell_ref: Mapped[str | None] = mapped_column(Text, comment="block ref | zone name")
    geom: Mapped[Any] = mapped_column(multipolygon(), nullable=False)
    value: Mapped[float] = mapped_column(Float(53), nullable=False)
    value_low: Mapped[float | None] = mapped_column(
        Float(53),
        comment="sale_price: low sale rate (absolute bound, else expected x low factor)",
    )
    value_high: Mapped[float | None] = mapped_column(
        Float(53),
        comment="sale_price: high sale rate (absolute bound, else expected x high factor)",
    )
    value_band: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="the cell's legend row: number of breaks <= value; sale_price: 0 = not "
        "saleable, then 1 + that number",
    )
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(
        Text, comment="height: the floor notation as the plan prints it"
    )
    parcel_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="planned parcels the value comes from",
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False, comment="planning | assumptions")
    dataset_version: Mapped[str | None] = mapped_column(
        Text, comment="label of the publish version"
    )
    assumptions_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("financial_assumptions.id", ondelete="SET NULL"),
        comment="sale_price: the financial_assumptions version the value comes from",
    )
    assumptions_version: Mapped[str | None] = mapped_column(
        Text, comment="sale_price: that version's number in the zone's history"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "publish_version_id", "layer", "cell_id", name="uq_choropleth_cells_version_layer_cell"
        ),
        CheckConstraint(_LAYER_CHECK, name="ck_choropleth_cells_layer"),
        CheckConstraint("cell_type IN ('block', 'zone')", name="ck_choropleth_cells_type"),
        CheckConstraint(
            "source_kind IN ('planning', 'assumptions')", name="ck_choropleth_cells_source"
        ),
        gist_index("choropleth_cells", "geom"),
    )


class ChoroplethClass(Base):
    __tablename__ = "choropleth_classes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(Text, nullable=False)
    publish_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_versions.id", ondelete="CASCADE"), nullable=False
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False, comment="quantile | fixed")
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    breaks: Mapped[list[float]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="class starts after the first class, ascending: v is in class = number of "
        "breaks <= v",
    )
    min: Mapped[float | None] = mapped_column(Float(53))
    max: Mapped[float | None] = mapped_column(Float(53))
    mean: Mapped[float | None] = mapped_column(Float(53))
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="cells with a value"
    )
    null_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="blocks / zones without a value: drawn as not covered",
    )
    zero_class: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="0 is its own class first (sale price: not saleable)",
    )
    decimals: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("publish_version_id", "layer", name="uq_choropleth_classes_version_layer"),
        CheckConstraint(_LAYER_CHECK, name="ck_choropleth_classes_layer"),
        CheckConstraint("method IN ('quantile', 'fixed')", name="ck_choropleth_classes_method"),
    )
