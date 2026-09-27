"""The cadastral base loader without a database (core.cadastre): profile, access gate, adapters,
attribute mapping, ogr2ogr on the sample extract, the report.

The sample extracts (``tests/cadastre/``) are ETRS89 / UTM 34N exports shaped like the profile's
UZN placeholders, a few metres from Njegoševa in Podgorica.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.cadastre import adapters as A
from core.cadastre.config import CadastreProfile, SourceConfig, load_cadastre_profile
from core.cadastre.normalise import (
    canonical_ko,
    clean,
    flag,
    map_ownership,
    map_parcel,
    split_number,
)
from core.cadastre.ogr import Ogr, OgrError, dataset_name, find_ogr2ogr, read_features
from core.cadastre.report import render_markdown

SAMPLES = Path(__file__).parent / "cadastre"


def profile(**changes: object) -> CadastreProfile:
    data: dict[str, object] = {
        "source": "uzn_geoportal",
        "ownership_source": "ekatastar",
        "ko_names": {"PODGORICA 2": "Podgorica II", "907": "Podgorica II"},
        "sources": {
            "uzn_geoportal": {
                "name": "Geoportal UZN",
                "url": "https://webmap.uzn.me/geoportal01",
                "kind": "parcels",
                "access": "confirmed",
                "access_basis": "test agreement",
                "licence_note": "test licence",
                "methods": ["file", "wfs"],
                "fields": {
                    "ko_code": "KO_SIFRA",
                    "ko_name": "KO_NAZIV",
                    "parcel_number": "BROJ_PARC",
                    "sub_number": "PODBROJ",
                    "street_address": ["ULICA", "KUCNI_BROJ"],
                    "fid": "FID_UZN",
                },
            },
            "ekatastar": {
                "name": "eKatastar",
                "url": "https://ekatastar.me",
                "kind": "ownership",
                "ownership_fields": {
                    "ko_name": "KO",
                    "parcel_number": "BROJ",
                    "public_ownership": "JAVNA",
                    "restitution_or_legal_burden": "TERET",
                },
            },
        },
    }
    data.update(changes)
    return CadastreProfile.model_validate(data)


# --- profile ------------------------------------------------------------------------------------


def test_podgorica_profile_has_no_confirmed_source_yet() -> None:
    p = load_cadastre_profile("podgorica")
    assert p.source == "uzn_geoportal" and p.ownership_source == "ekatastar"
    assert {s.access for s in p.sources.values()} == {"not_confirmed"}
    assert p.area_crs_epsg == 25834
    assert p.sources["ekatastar"].methods == ["file"]  # never a web lookup


def test_confirmed_access_needs_its_basis_and_licence() -> None:
    with pytest.raises(ValidationError, match="access_basis"):
        SourceConfig(
            id="x",
            name="X",
            kind="parcels",
            access="confirmed",
            fields={"ko_name": "KO", "parcel_number": "N"},
        )
    with pytest.raises(ValidationError, match="method wfs is not in methods"):
        SourceConfig(
            id="x",
            name="X",
            kind="parcels",
            method="wfs",
            fields={"ko_name": "K", "parcel_number": "N"},
        )
    with pytest.raises(ValidationError, match="KO is mandatory"):
        SourceConfig(id="x", name="X", kind="parcels", fields={"parcel_number": "N"})
    with pytest.raises(ValidationError, match="not a parcel source"):
        profile(source="ekatastar")


# --- the access gate ----------------------------------------------------------------------------


def test_sources_without_confirmed_access_stop_with_the_reason(tmp_path: Path) -> None:
    p = load_cadastre_profile("podgorica")
    for sid in ("uzn_geoportal", "emapa", "ekatastar"):
        adapter = A.adapter_for(p, sid, "podgorica")
        with pytest.raises(A.AccessNotConfirmed) as err:
            adapter.acquire(tmp_path, path=SAMPLES / "parcels_v1.geojson")
        message = str(err.value)
        assert "not confirmed" in message and "no scraping fallback" in message
        assert f"[cadastre.sources.{sid}]" in message and "podgorica.toml" in message
    assert isinstance(A.adapter_for(p, "ekatastar", "podgorica"), A.EkatastarOwnershipSource)
    assert isinstance(A.adapter_for(p, "emapa", "podgorica"), A.OgrParcelSource)


def test_a_confirmed_file_source_records_its_provenance(tmp_path: Path) -> None:
    adapter = A.adapter_for(profile(), "uzn_geoportal", "podgorica")
    export = SAMPLES / "parcels_v1.geojson"
    when = datetime(2026, 10, 2, tzinfo=UTC)
    record = adapter.acquire(tmp_path, path=export, retrieved_at=when)
    assert record.file_sha256 == hashlib.sha256(export.read_bytes()).hexdigest()
    assert record.file_size == export.stat().st_size and record.file_name == export.name
    assert record.retrieved_at == when and record.method == "file"
    assert (record.access_basis, record.licence_note) == ("test agreement", "test licence")
    assert record.as_json()["retrieved_at"] == "2026-10-02T00:00:00+00:00"
    now = adapter.acquire(tmp_path, path=export).retrieved_at
    assert now.tzinfo is not None and (datetime.now(UTC) - now).total_seconds() < 60
    with pytest.raises(A.SourceError, match="no such export"):
        adapter.acquire(tmp_path, path=tmp_path / "missing.zip")


def test_ekatastar_takes_only_a_delivered_export(tmp_path: Path) -> None:
    p = profile(
        sources={
            **{k: v.model_dump() for k, v in profile().sources.items() if k != "ekatastar"},
            "ekatastar": {
                **profile().sources["ekatastar"].model_dump(),
                "access": "confirmed",
                "access_basis": "agreement",
                "licence_note": "licence",
            },
        }
    )
    adapter = A.adapter_for(p, "ekatastar", "podgorica")
    with pytest.raises(A.SourceError, match="no bulk service"):
        adapter.acquire(tmp_path)
    export = tmp_path / "flags.csv"
    export.write_text("KO,BROJ,JAVNA,TERET\nTest KO Alpha,1234/5,DA,NE\n", encoding="utf-8")
    assert (
        adapter.acquire(tmp_path, path=export).file_sha256
        == hashlib.sha256(export.read_bytes()).hexdigest()
    )


# --- attribute mapping --------------------------------------------------------------------------


def test_numbers_kos_and_addresses_are_mapped_as_delivered() -> None:
    assert clean(1234.0) == "1234" and clean(" 12  a ") == "12 a" and clean("") is None
    assert split_number("1234/5", None, "/") == ("1234", "5")
    assert split_number("1234/5", "7", "/") == ("1234/5", "7")  # a sub-number field wins
    assert split_number("1234", None, "/") == ("1234", None)
    names = {"PODGORICA 2": "Podgorica II", "907": "Podgorica II"}
    assert canonical_ko("PODGORICA 2", None, names) == ("Podgorica II", None)
    assert canonical_ko(None, "907", names) == ("Podgorica II", "907")
    assert canonical_ko("Tološi", "908", names) == ("Tološi", "908")
    fields = profile().sources["uzn_geoportal"].fields
    rec = map_parcel(
        {
            "KO_SIFRA": 907,
            "KO_NAZIV": None,
            "BROJ_PARC": 1234.0,
            "PODBROJ": 5,
            "ULICA": "Njegoševa",
            "KUCNI_BROJ": None,
            "FID_UZN": 77,
        },
        fields,
        names,
        row=3,
    )
    assert (rec.ko_name, rec.ko_code, rec.parcel_number, rec.sub_number) == (
        "Podgorica II",
        "907",
        "1234",
        "5",
    )
    assert rec.street_address == "Njegoševa" and rec.source_fid == "77" and rec.row == 3
    missing = map_parcel({"BROJ_PARC": "9"}, fields, names, row=1)
    assert missing.ko_name is None  # never guessed: the validation reports it


def test_flags_come_only_from_explicit_values() -> None:
    yes, no = ["DA", "1"], ["NE", "0"]
    assert flag("da", yes, no) is True and flag(0, yes, no) is False
    assert flag("djelimično", yes, no) is None and flag(None, yes, no) is None
    fields = profile().sources["ekatastar"].ownership_fields
    rec = map_ownership({"KO": "Test KO Alpha", "BROJ": "1234/5", "JAVNA": "true"}, fields, {})
    assert (rec.parcel_number, rec.sub_number, rec.public_ownership) == ("1234", "5", True)
    assert rec.restitution_or_legal_burden is None  # no value: unknown, not false


# --- ogr2ogr ------------------------------------------------------------------------------------


def test_zip_exports_open_through_vsizip() -> None:
    assert dataset_name(Path("C:/data/export.zip")) == "/vsizip/C:/data/export.zip"
    assert dataset_name(Path("export.shp.zip")) == "export.shp.zip"
    assert dataset_name(Path("export.gpkg")) == "export.gpkg"


needs_gdal = pytest.mark.skipif(find_ogr2ogr() is None, reason="ogr2ogr not available")


@needs_gdal
def test_ogr2ogr_reprojects_the_sample_to_lon_lat(tmp_path: Path) -> None:
    ogr = Ogr()
    source = dataset_name(SAMPLES / "parcels_v1.geojson")
    (layer,) = ogr.layers(source)
    assert layer.crs == "EPSG:25834" and layer.feature_count == 6
    assert "BROJ_PARC" in layer.fields
    options = ogr.to_geojsonseq(source, tmp_path / "p.geojsonl", layer=layer.name)
    assert options[:2] == ["-t_srs", "EPSG:4326"] and "PROMOTE_TO_MULTI" in options
    features = list(read_features(tmp_path / "p.geojsonl"))
    assert len(features) == 6
    props, geometry = features[0]
    assert props["KO_NAZIV"] == "Test KO Alpha"
    lng, lat = json.loads(geometry)["coordinates"][0][0][0]
    # UTM 34N (356800, 4700300) is 19.2588 E, 42.4418 N: longitude first, 9 decimals
    assert lng == pytest.approx(19.25883, abs=1e-5) and lat == pytest.approx(42.44181, abs=1e-5)
    attributes = tmp_path / "flags.csv"
    attributes.write_text("KO,BROJ,JAVNA\nTest KO Alpha,1234/5,DA\n", encoding="utf-8")
    ogr.to_geojsonseq(str(attributes), tmp_path / "f.geojsonl", geometry=False)
    ((props, geometry),) = list(read_features(tmp_path / "f.geojsonl"))
    assert props == {"KO": "Test KO Alpha", "BROJ": "1234/5", "JAVNA": "DA"} and geometry is None


@needs_gdal
def test_ogr_errors_carry_gdal_s_message(tmp_path: Path) -> None:
    with pytest.raises(OgrError, match="ogrinfo failed"):
        Ogr().layers(str(tmp_path / "nothing.gpkg"))


# --- report ---------------------------------------------------------------------------------------


def test_report_states_provenance_validation_diff_and_the_ownership_decision() -> None:
    report = {
        "dataset_version": "cad-20261006-1",
        "status": "staged",
        "provenance": {
            "source_name": "Geoportal UZN",
            "method": "file",
            "source_url": "https://webmap.uzn.me/geoportal01",
            "retrieved_at": "2026-10-02T00:00:00+00:00",
            "access_basis": "agreement 01-123",
            "licence_note": "internal use",
            "file_name": "export.zip",
            "file_size": 2_500_000,
            "file_sha256": "ab" * 32,
            "source_crs": "EPSG:25834",
            "transform": None,
        },
        "area_srid": 25834,
        "ownership": {"status": "not_available", "summary": "not loaded: access not confirmed"},
        "validation": {
            "ok": True,
            "errors": [],
            "warnings": [
                {
                    "code": "repaired",
                    "message": "invalid geometries repaired",
                    "count": 1,
                    "samples": [{"row_no": 3, "parcel_number": "1235"}],
                }
            ],
            "stats": {
                "records": 6,
                "kos": [{"ko_name": "Test KO Alpha", "ko_code": "901", "parcels": 4}],
                "coverage": {"cell_m": 500, "cells": 10, "covered": 9, "ratio": 0.9},
                "repaired": 1,
                "outside_extent": 0,
            },
        },
        "diff": {
            "previous_version": "cad-20261001-1",
            "totals": {"added": 1, "removed": 1, "unchanged": 4},
            "csv_rows": 2,
        },
    }
    md = render_markdown(report)
    assert "# Cadastral dataset cad-20261006-1: staged" in md
    assert "agreement 01-123" in md and "EPSG:25834" in md and "2.4 MB" in md
    assert "not loaded: access not confirmed" in md
    assert "`repaired` invalid geometries repaired: 1" in md
    assert "| removed (retired at publish) | 1 |" in md and "diff.csv" in md
    assert "90.0%" in md and "| Test KO Alpha | 901 | 4 |" in md
