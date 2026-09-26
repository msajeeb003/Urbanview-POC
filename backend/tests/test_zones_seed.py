"""The zone document seed list (core/zones/seed.py): the reference capture, the eRegistri snapshot,
the name matching and the CSVs the client's QGIS session starts from."""

from __future__ import annotations

import csv
import dataclasses
import json

import httpx
import pytest

from core.zones.config import load_zone_config
from core.zones.schema import DOCUMENT_FIELD_NAMES, ZONE_FIELD_NAMES
from core.zones.seed import (
    ListedDocument,
    RegistryEntry,
    build_seed,
    gazette_year,
    load_registry,
    match_registry,
    name_tokens,
    parse_capture,
    read_csv_rows,
    write_seed,
)


@pytest.fixture(scope="module")
def cfg():
    return load_zone_config("podgorica")


@pytest.fixture(scope="module")
def entries(cfg):
    return load_registry(cfg.eregistri)


@pytest.fixture(scope="module")
def seed(cfg):
    return build_seed(cfg)


def _doc(listing: str, badge: str = "DUP", year: int | None = None, zone: str = "Z"):
    return ListedDocument(zone, badge, listing, listing, year, 1)


def _entry(id: str, name: str, year: int | None, type_key: str = "DUP") -> RegistryEntry:
    return RegistryEntry(id, id, name, "", type_key, "", year, "", "", (), "", False)


def test_parse_capture_reads_every_zone_and_document(cfg):
    zones = parse_capture(cfg.reference.capture)
    assert len(zones) == 11
    assert sum(len(z.documents) for z in zones) == 138  # the header's "<type> | ..." is a comment
    assert [z.name for z in zones][:2] == ["Nova Varoš", "Novi Grad (Preko Morače)"]
    docs = {d.listing: d for z in zones for d in z.documents}
    momisici = docs["DUP Momišići C (2009)"]
    assert (momisici.badge, momisici.name, momisici.year) == ("DUP", "DUP Momišići C", 2009)
    assert momisici.zone_name == "Novi Grad (Preko Morače)"
    assert docs["DUP Privaj Maj – izmjene i dopune"].year is None


def test_parse_capture_refuses_what_it_cannot_place(tmp_path):
    path = tmp_path / "capture.txt"
    path.write_text("# comment\nDUP | DUP Orphan (2001)\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":2: document before"):
        parse_capture(path)
    path.write_text("## Zone\nDUP Without badge separator\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":2: expected"):
        parse_capture(path)


@pytest.mark.parametrize(
    ("gazette", "year"),
    [
        ("Sl. list CG br. 64/08", 2008),
        ("Sl.list RCG br.46/01", 2001),
        ("Sl.list CG - opštinski propisi, broj 6/2014", 2014),
        ('"Službeni list Crne Gore", br. 068/25', 2025),
        ("Sl.list - opštinski propisi br. 032/18 od 18.09.2018", 2018),
        ("", None),
    ],
)
def test_gazette_year(gazette, year):
    assert gazette_year(gazette) == year


def test_registry_snapshot(entries):
    assert len(entries) == 156
    by_id = {e.id: e for e in entries}
    assert len(by_id) == 156
    kap = by_id["4178"]
    assert kap.invalid and kap.invalid_marker == "NEVAŽEĆI"
    assert by_id["4304"].invalid  # "PREDMETNI PLAN NIJE VAŽEĆI !!! ..."
    assert not by_id["9698"].invalid and by_id["9698"].note.startswith("PO POSEBNOM POSTUPKU")
    autoput = by_id["4382"]
    assert (autoput.type_key, autoput.gazette_year) == ("DPP", 2008)  # "Detaljni prostroni plan"
    assert by_id["5594"].cadastral_municipalities == ("PODGORICA I", "TOLOŠI")


def test_refresh_makes_one_request_and_keeps_the_snapshot(tmp_path, cfg):
    reg = dataclasses.replace(cfg.eregistri, snapshot=tmp_path / "source" / "registry.json")
    payload = {
        "records": 1,
        "rows": [{"id": "7", "cell": ["", "", "PG/1/2020", "DUP - Nova Varoš", "", "", "",
                                      "Detaljni urbanistički plan", "br. 9/20", "", "", "", "",
                                      "PLANKI DOKUMENT JE NEVAŽEĆI"]}],
    }  # fmt: skip
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    [entry] = load_registry(reg, refresh=True, get=fake_get)
    assert len(calls) == 1 and calls[0][0] == reg.list_url
    assert "UrbanView" in calls[0][1]["headers"]["User-Agent"]
    assert calls[0][1]["timeout"] == 60
    assert (entry.id, entry.type_key, entry.gazette_year, entry.invalid) == ("7", "DUP", 2020, True)
    text = reg.snapshot.read_text(encoding="utf-8")
    assert "Nova Varoš" in text  # ensure_ascii off
    assert load_registry(reg) == [entry]

    payload["records"] = 400  # a partial list never replaces the snapshot
    with pytest.raises(ValueError, match="1 of 400"):
        load_registry(reg, refresh=True, get=fake_get)
    assert reg.snapshot.read_text(encoding="utf-8") == text


def test_matching_real_names(entries):
    docs = [
        _doc("DUP Novi Grad 1 i 2 – izmjene i dopune", year=2006),
        _doc("DUP Stambena zajednica 6 – Kruševac – izmjene i dopune", year=2004),
        _doc("DUP Konik – Stari Aerodrom III", year=2012),
        _doc("DUP Nova Varoš – izmjene i dopune", year=2006),  # not "Nova Varoš 2"
    ]
    novi_grad, sz6, konik, nova_varos = match_registry(docs, entries, threshold=0.72)
    assert novi_grad.entry.name == "DUP - Novi grad 1 i 2 - Izmjene i dopune"
    assert novi_grad.method == "exact" and novi_grad.score == 1.0
    assert sz6.entry.name == "DUP - Stambena zajednica VI Kruševac" and sz6.method == "fuzzy"
    assert konik.entry.id == "4156"  # "Konik-Stari Aerodrom faza 3", gazette 2012
    assert nova_varos is None
    drop = {"dup"}
    assert name_tokens("DUP - Stambena zajednica VI", drop) == name_tokens(
        "DUP Stambena zajednica 6", drop
    )


def test_matching_settles_amendments_by_year_and_respects_types():
    entries = [
        _entry("a", "DUP - Blok 5 - Izmjene i dopune", 2010),
        _entry("b", "DUP - Blok 5 - Izmjene i dopune", 2012),
        _entry("c", "UP - Kasarna", 2007, type_key="UP"),
    ]
    docs = [
        _doc("DUP Blok 5 – izmjene i dopune", year=2012),
        _doc("DUP Blok 5 – izmjene i dopune", year=2010),
        _doc("DUP Blok 5 – izmjene i dopune", year=2001),  # nothing left for it
        _doc("DUP Kasarna", year=2007),  # listed as DUP, registered as UP
    ]
    got = match_registry(docs, entries, threshold=0.72)
    assert [m.entry.id if m else None for m in got] == ["b", "a", None, None]


def test_build_seed_suggestions(seed, cfg):
    s = seed.stats
    assert (s["zones"], s["documents"]) == (11, 138)
    assert s["matched_exact"] + s["matched_fuzzy"] + s["unmatched"] == 138
    assert s["registry_unlisted"] == 156 - s["matched_exact"] - s["matched_fuzzy"]
    assert [z["zone_id"] for z in seed.zones] == list(cfg.reference.zone_ids.values())
    assert all(list(z) == list(ZONE_FIELD_NAMES) for z in seed.zones)
    assert all(list(r) == list(DOCUMENT_FIELD_NAMES) for r in seed.documents)

    refs = [r["eregistri_reference"] for r in seed.documents if r["eregistri_reference"]]
    assert len(refs) == len(set(refs))  # no registry entry taken twice
    assert all(r["adoption_date"] == "" for r in seed.documents)

    by_listing = {r["listed_as"]: r for r in seed.documents}
    invalid = by_listing["DUP Servisno-skladišna zona – izmjene i dopune (2009)"]
    assert (invalid["status"], invalid["eregistri_reference"]) == ("superseded", "4304")
    assert "NIJE VAŽEĆI" in invalid["notes"]
    matched = by_listing["DUP Momišići C (2009)"]
    assert (matched["status"], matched["match"]) == ("adopted", "exact")
    assert matched["source_url"] == "https://lamp.gov.me/PlanningDocument/Details/4159"
    assert matched["document_name"] == "DUP Momišići C" and matched["listed_year"] == 2009
    old = by_listing["DUP Park šuma Gorica (1995)"]
    assert (old["status"], old["match"], old["source_url"]) == ("adopted", "none", "")
    assert "not found in eRegistri" in old["notes"]
    planned = by_listing["DUP Tuški put"]
    assert planned["status"] == "in_progress" and "in preparation" in planned["notes"]
    moved = by_listing["DUP Novi Grad 1 i 2 – izmjene i dopune (2006)"]
    assert "listed 2006, eRegistri gazette 2012" in moved["notes"]
    assert any(w.startswith("eRegistri only: PG/64/2016") for w in seed.warnings)


def test_write_seed_round_trip_and_guard(tmp_path, cfg, seed):
    local = dataclasses.replace(
        cfg, zones_seed=tmp_path / "zones.csv", documents=tmp_path / "zone_documents.csv"
    )
    assert write_seed(local, seed) == [local.zones_seed, local.documents]
    assert local.documents.read_bytes().startswith(b"\xef\xbb\xbf")  # Excel reads č ć š ž đ
    rows = read_csv_rows(local.documents)
    assert len(rows) == 138 and list(rows[0]) == list(DOCUMENT_FIELD_NAMES)
    assert rows[0]["poc_coverage"] == "false" and rows[0]["confirmed"] == "false"
    assert {r["listed_as"] for r in rows} == {r["listed_as"] for r in seed.documents}
    assert read_csv_rows(local.zones_seed)[0]["no_adopted_plan"] == "false"

    # the client confirmed a row (Excel, Montenegrin locale: ";" separated)
    rows[0]["confirmed"] = "da"
    with local.documents.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    assert read_csv_rows(local.documents)[0]["confirmed"] == "da"
    with pytest.raises(FileExistsError, match="1 confirmed"):
        write_seed(local, seed)
    write_seed(local, seed, overwrite=True)
    assert read_csv_rows(local.documents)[0]["confirmed"] == "false"


def test_snapshot_is_valid_json(cfg):
    # the checked-in snapshot is what every offline seed run reads
    data = json.loads(cfg.eregistri.snapshot.read_text(encoding="utf-8"))
    assert data["records"] == len(data["rows"])
