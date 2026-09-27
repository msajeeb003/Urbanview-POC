"""Attribute mapping of an export's records (pure functions, no database, no GDAL).

The parcel number is kept as the cadastre writes it (text, trimmed); an export that writes the
sub-number into the number field ("1234/5") is split at the configured separator. The KO is kept
as delivered unless the profile maps the source's name or code to the official name
(``[cadastre] ko_names``). Nothing is guessed: a missing KO or number stays missing and the
validation reports the record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.cadastre.config import OwnershipFields, ParcelFields


def clean(value: Any) -> str | None:
    """A field value as trimmed text; None for empty. Integral floats lose their '.0' (a
    Shapefile stores whole numbers in real fields)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        value = int(value) if value.is_integer() else value
    text = " ".join(str(value).split())
    return text or None


def split_number(
    number: str | None, sub: str | None, separator: str
) -> tuple[str | None, str | None]:
    if number and sub is None and separator and separator in number:
        head, _, tail = number.partition(separator)
        return (head.strip() or None, tail.strip() or None)
    return number, sub


def canonical_ko(
    name: str | None, code: str | None, ko_names: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """(official name, code): the profile's map by name first, then by code; else as delivered."""
    for key in (name, code):
        if key is not None and key in ko_names:
            return ko_names[key], code
    return name, code


@dataclass(frozen=True, slots=True)
class ParcelRecord:
    row: int
    source_fid: str | None
    ko_code: str | None
    ko_name: str | None
    parcel_number: str | None
    sub_number: str | None
    street_address: str | None


def map_parcel(
    props: Mapping[str, Any], fields: ParcelFields, ko_names: Mapping[str, str], row: int
) -> ParcelRecord:
    number, sub = split_number(
        clean(props.get(fields.parcel_number)),
        clean(props.get(fields.sub_number)) if fields.sub_number else None,
        fields.number_separator,
    )
    ko_name, ko_code = canonical_ko(
        clean(props.get(fields.ko_name)) if fields.ko_name else None,
        clean(props.get(fields.ko_code)) if fields.ko_code else None,
        ko_names,
    )
    address = " ".join(
        v for v in (clean(props.get(f)) for f in fields.street_address) if v is not None
    )
    fid = props.get(fields.fid) if fields.fid else None
    return ParcelRecord(
        row=row,
        source_fid=clean(fid),
        ko_code=ko_code,
        ko_name=ko_name,
        parcel_number=number,
        sub_number=sub,
        street_address=address or None,
    )


def flag(value: Any, true_values: list[str], false_values: list[str]) -> bool | None:
    """An explicit flag value of the export; anything not listed stays unknown (None)."""
    text = clean(value)
    if text is None:
        return None
    folded = text.casefold()
    if folded in {v.casefold() for v in true_values}:
        return True
    if folded in {v.casefold() for v in false_values}:
        return False
    return None


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    ko_name: str | None
    ko_code: str | None
    parcel_number: str | None
    sub_number: str | None
    public_ownership: bool | None
    restitution_or_legal_burden: bool | None


def map_ownership(
    props: Mapping[str, Any], fields: OwnershipFields, ko_names: Mapping[str, str]
) -> OwnershipRecord:
    number, sub = split_number(
        clean(props.get(fields.parcel_number)),
        clean(props.get(fields.sub_number)) if fields.sub_number else None,
        fields.number_separator,
    )
    ko_name, ko_code = canonical_ko(
        clean(props.get(fields.ko_name)) if fields.ko_name else None,
        clean(props.get(fields.ko_code)) if fields.ko_code else None,
        ko_names,
    )

    def read(column: str | None) -> bool | None:
        if column is None:
            return None
        return flag(props.get(column), fields.true_values, fields.false_values)

    return OwnershipRecord(
        ko_name=ko_name,
        ko_code=ko_code,
        parcel_number=number,
        sub_number=sub,
        public_ownership=read(fields.public_ownership),
        restitution_or_legal_burden=read(fields.restitution_or_legal_burden),
    )
