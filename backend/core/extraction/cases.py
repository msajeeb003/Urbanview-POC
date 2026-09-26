"""The "never guessed" cases: pages where a field is absent and a plausible value is within
reach (a floor area ratio from coverage x floors, a height from the floor count, an area from gross
floor area / FAR, a neighbour's parking rule, an adoption date from the plan's date).

Shared by the unit tests (with scripted responses: a faithful one and a guessing one) and the
live evaluation (``python -m core.extraction eval``), which asks the configured model and passes a
case only when the model itself returns null (``not_found``) for every absent field.

Also the builders of model responses (:func:`v`, :func:`nf`, :func:`deferred`, :func:`rules`,
:func:`parcel`, :func:`block`) and of synthetic pages (:func:`text_page`) the tests use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.extraction.fields import RULE_FIELDS
from core.extraction.pages import PageInput, Word
from core.extraction.schema import ExtractionResult, Leaf, TaskKind
from core.extraction.textmatch import words_fold

# --- response builders (what a model returns) ----------------------------------------------------


def v(
    value: str,
    raw: str | None = None,
    *,
    page: int = 1,
    unit: str | None = None,
    confidence: float = 0.95,
    row: str | None = None,
    column: str | None = None,
    cell: str | None = None,
    table: str | None = None,
) -> dict[str, Any]:
    ref = None
    if any((row, column, cell, table)):
        ref = {"table": table, "row": row, "column": column, "cell": cell}
    return {
        "value": value,
        "unit": unit,
        "raw_text": value if raw is None else raw,
        "page": page,
        "table_ref": ref,
        "confidence": confidence,
        "absent_reason": None,
    }


def nf() -> dict[str, Any]:
    return {
        "value": None,
        "unit": None,
        "raw_text": None,
        "page": None,
        "table_ref": None,
        "confidence": 1.0,
        "absent_reason": "not_found",
    }


def deferred(raw: str, *, page: int = 1, row: str | None = None) -> dict[str, Any]:
    out = nf() | {"raw_text": raw, "page": page, "absent_reason": "deferred"}
    if row:
        out["table_ref"] = {"table": None, "row": row, "column": None, "cell": None}
    return out


def rules(**given: dict[str, Any]) -> dict[str, Any]:
    return {key: given.get(key, nf()) for key in RULE_FIELDS}


def parcel(
    number: dict[str, Any],
    *,
    block: dict[str, Any] | None = None,
    area: dict[str, Any] | None = None,
    relation: dict[str, Any] | None = None,
    other: list[dict[str, Any]] | None = None,
    **rule_values: dict[str, Any],
) -> dict[str, Any]:
    return {
        "urban_parcel_number": number,
        "block_ref": block or nf(),
        "planned_parcel_area_m2": area or nf(),
        "rules": rules(**rule_values),
        "public_area_relation": relation or nf(),
        "other_conditions": other or [],
    }


def block(
    ref: dict[str, Any],
    *,
    total: bool = False,
    area: dict[str, Any] | None = None,
    notes: list[dict[str, Any]] | None = None,
    **rule_values: dict[str, Any],
) -> dict[str, Any]:
    return {
        "block_ref": ref,
        "is_total_row": total,
        "area_m2": area or nf(),
        "rules": rules(**rule_values),
        "notes": notes or [],
    }


def document(**given: Any) -> dict[str, Any]:
    keys = (
        "name",
        "document_type",
        "status",
        "gazette_reference",
        "decision_number",
        "decision_date",
        "area_ha",
    )
    out: dict[str, Any] = {key: given.get(key, nf()) for key in keys}
    out["amendments"] = given.get("amendments", [])
    out["rules"] = given.get("rules", rules())
    out["notes"] = given.get("notes", [])
    return {"document": out}


# --- synthetic pages -----------------------------------------------------------------------------

LINE_HEIGHT, CHAR_WIDTH, TOP = 14.0, 5.5, 800.0


def text_page(number: int, text: str) -> PageInput:
    """A page whose words sit on one line per text line (PDF points, origin bottom-left)."""
    words: list[Word] = []
    for i, line in enumerate(text.splitlines()):
        top = TOP - i * LINE_HEIGHT
        x = 50.0
        for token in line.split():
            width = len(token) * CHAR_WIDTH
            words.append(Word(token, (x, top - 10.0, x + width, top)))
            x += width + CHAR_WIDTH
    return PageInput(page=number, text=text, words=tuple(words))


# --- finding a leaf in a result ------------------------------------------------------------------


def find_leaf(result: ExtractionResult, where: str) -> Leaf | None:
    """``parcel:14/rules.max_far``, ``block:a/area_m2`` or ``document/decision_date``."""
    head, _, path = where.partition("/")
    kind, _, key = head.partition(":")
    target: Any
    if kind == "document":
        target = result.document
    elif kind == "parcel":
        target = next((p for p in result.urban_parcels if p.parcel_key == key), None)
    elif kind == "block":
        target = next((b for b in result.blocks if b.block_key == words_fold(key)), None)
    else:
        raise ValueError(f"not a leaf address: {where}")
    for part in path.split("."):
        if target is None:
            return None
        target = getattr(target, part)
    return target


# --- the cases -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AbsentCase:
    id: str
    trap: str
    task: TaskKind
    pages: tuple[PageInput, ...]
    absent: tuple[str, ...]  # leaves the model must return as not_found
    stated: dict[str, float | str] = field(default_factory=dict)  # leaves the page states
    faithful: dict[str, Any] = field(default_factory=dict)  # a correct response
    guessing: dict[str, Any] = field(default_factory=dict)  # a plausible guess


_UP14 = """4.2. Urbanističko-tehnički uslovi
Urbanistička parcela UP 14
Namjena: stanovanje sa djelatnostima
Površina urbanističke parcele: 1.240,50 m2
Maksimalni indeks zauzetosti: 0,40
Maksimalna spratnost objekta: P+4
Građevinska linija je udaljena 5,0 m od regulacione linije.
Parkiranje riješiti na parceli: 1 PM na 1 stan."""


def _case_far() -> AbsentCase:
    page = text_page(3, _UP14)
    known = {
        "number": v("UP 14", "Urbanistička parcela UP 14", page=3),
        "land_use": v(
            "stanovanje sa djelatnostima", "Namjena: stanovanje sa djelatnostima", page=3
        ),
        "area": v("1.240,50 m2", "Površina urbanističke parcele: 1.240,50 m2", page=3, unit="m2"),
        "coverage": v("0,40", "Maksimalni indeks zauzetosti: 0,40", page=3, unit="ratio"),
        "floors": v("P+4", "Maksimalna spratnost objekta: P+4", page=3),
        "line": v(
            "5,0 m",
            "Građevinska linija je udaljena 5,0 m od regulacione linije.",
            page=3,
            unit="m",
        ),
        "parking": v("1 PM na 1 stan", "Parkiranje riješiti na parceli: 1 PM na 1 stan.", page=3),
    }

    def response(far: dict[str, Any], height: dict[str, Any]) -> dict[str, Any]:
        return {
            "urban_parcels": [
                parcel(
                    known["number"],
                    area=known["area"],
                    land_use=known["land_use"],
                    max_site_coverage_pct=known["coverage"],
                    max_floors=known["floors"],
                    building_line_m=known["line"],
                    parking_requirement=known["parking"],
                    max_far=far,
                    max_height_m=height,
                )
            ],
            "blocks": [],
            "columns": [],
        }

    return AbsentCase(
        id="far_from_coverage_and_floors",
        trap="coverage 0,40 on P+4 invites FAR 2,0 and a 15 m height",
        task="urban_parcel",
        pages=(page,),
        absent=("parcel:14/rules.max_far", "parcel:14/rules.max_height_m"),
        stated={
            "parcel:14/planned_parcel_area_m2": 1240.5,
            "parcel:14/rules.max_site_coverage_pct": 40.0,
            "parcel:14/rules.max_floors": "P+4",
            "parcel:14/rules.building_line_m": 5.0,
        },
        faithful=response(nf(), nf()),
        guessing=response(
            v("2,0", "Maksimalni indeks zauzetosti: 0,40", page=3, unit="ratio"),
            v("15 m", "Maksimalna spratnost objekta: P+4", page=3, unit="m"),
        ),
    )


_TABLE = """Tabela 3. Urbanistički parametri po parcelama
Blok
Broj UP
Površina UP (m2)
Namjena
Spratnost
Indeks zauzetosti
Indeks izgrađenosti
B
UP 21
890,49
stanovanje
P+4+M
0,50
2,50
B
UP 22
1404,60
stanovanje sa djelatn.
Po+P+7
0,60
4,20"""


def _case_height() -> AbsentCase:
    page = text_page(7, _TABLE)

    def row(
        number: str, area: str, use: str, floors: str, iz: str, ii: str, height: dict[str, Any]
    ) -> dict[str, Any]:
        cell: dict[str, Any] = {"page": 7, "row": number}
        return parcel(
            v(number, **cell),
            block=v("B", **cell),
            area=v(area, unit="m2", column="Površina UP (m2)", **cell),
            land_use=v(use, column="Namjena", **cell),
            max_floors=v(floors, column="Spratnost", **cell),
            max_site_coverage_pct=v(iz, unit="ratio", column="Indeks zauzetosti", **cell),
            max_far=v(ii, unit="ratio", column="Indeks izgrađenosti", **cell),
            max_height_m=height,
        )

    def response(h21: dict[str, Any], h22: dict[str, Any]) -> dict[str, Any]:
        return {
            "urban_parcels": [
                row("UP 21", "890,49", "stanovanje", "P+4+M", "0,50", "2,50", h21),
                row("UP 22", "1404,60", "stanovanje sa djelatn.", "Po+P+7", "0,60", "4,20", h22),
            ],
            "blocks": [],
            "columns": [],
        }

    return AbsentCase(
        id="height_from_floors",
        trap="a table with floors (P+4+M, Po+P+7) and no height column invites 18 m / 24 m",
        task="parameter_table",
        pages=(page,),
        absent=("parcel:21/rules.max_height_m", "parcel:22/rules.max_height_m"),
        stated={
            "parcel:21/rules.max_site_coverage_pct": 50.0,
            "parcel:22/rules.max_far": 4.2,
            "parcel:22/rules.max_floors": "Po+P+7",
        },
        faithful=response(nf(), nf()),
        guessing=response(
            v("18,00", page=7, unit="m", row="UP 21", column="Visina"),
            v("24,00", page=7, unit="m", row="UP 22", column="Visina"),
        ),
    )


_UP31 = """Urbanistička parcela UP 31
Namjena: centralne djelatnosti
Maksimalna bruto građevinska površina (BRGP): 2.400 m2
Maksimalni indeks izgrađenosti: 2,0
Maksimalni indeks zauzetosti: 0,50
Maksimalna spratnost: S+P+3"""


def _case_area() -> AbsentCase:
    page = text_page(5, _UP31)

    def response(area: dict[str, Any]) -> dict[str, Any]:
        return {
            "urban_parcels": [
                parcel(
                    v("UP 31", "Urbanistička parcela UP 31", page=5),
                    area=area,
                    land_use=v("centralne djelatnosti", page=5),
                    max_far=v("2,0", "Maksimalni indeks izgrađenosti: 2,0", page=5, unit="ratio"),
                    max_site_coverage_pct=v(
                        "0,50", "Maksimalni indeks zauzetosti: 0,50", page=5, unit="ratio"
                    ),
                    max_floors=v("S+P+3", "Maksimalna spratnost: S+P+3", page=5),
                )
            ],
            "blocks": [],
            "columns": [],
        }

    return AbsentCase(
        id="area_from_gross_floor_area",
        trap="BRGP 2.400 m2 with FAR 2,0 invites a parcel area of 1.200 m2",
        task="urban_parcel",
        pages=(page,),
        absent=("parcel:31/planned_parcel_area_m2",),
        stated={"parcel:31/rules.max_far": 2.0, "parcel:31/rules.max_site_coverage_pct": 50.0},
        faithful=response(nf()),
        guessing=response(
            v(
                "1.200",
                "Maksimalna bruto građevinska površina (BRGP): 2.400 m2",
                page=5,
                unit="m2",
            )
        ),
    )


_UP7_UP8 = """Urbanistička parcela UP 7
Namjena: stanovanje
Maksimalni indeks zauzetosti: 0,40
Maksimalni indeks izgrađenosti: 1,60
Parkiranje: 1 PM na 1 stan, u garaži u suterenu.
Urbanistička parcela UP 8
Namjena: stanovanje
Maksimalni indeks zauzetosti: 0,35
Maksimalni indeks izgrađenosti: 1,40"""


def _case_parking() -> AbsentCase:
    page = text_page(9, _UP7_UP8)
    parking = "Parkiranje: 1 PM na 1 stan, u garaži u suterenu."

    def response(up8_parking: dict[str, Any]) -> dict[str, Any]:
        return {
            "urban_parcels": [
                parcel(
                    v("UP 7", "Urbanistička parcela UP 7", page=9),
                    land_use=v("stanovanje", "Namjena: stanovanje", page=9),
                    max_site_coverage_pct=v(
                        "0,40", "Maksimalni indeks zauzetosti: 0,40", page=9, unit="ratio"
                    ),
                    max_far=v("1,60", "Maksimalni indeks izgrađenosti: 1,60", page=9, unit="ratio"),
                    parking_requirement=v("1 PM na 1 stan, u garaži u suterenu", parking, page=9),
                ),
                parcel(
                    v("UP 8", "Urbanistička parcela UP 8", page=9),
                    land_use=v("stanovanje", "Namjena: stanovanje", page=9),
                    max_site_coverage_pct=v(
                        "0,35", "Maksimalni indeks zauzetosti: 0,35", page=9, unit="ratio"
                    ),
                    max_far=v("1,40", "Maksimalni indeks izgrađenosti: 1,40", page=9, unit="ratio"),
                    parking_requirement=up8_parking,
                ),
            ],
            "blocks": [],
            "columns": [],
        }

    return AbsentCase(
        id="parking_from_neighbour",
        trap="UP 7 states a parking rule, UP 8 on the same page does not",
        task="urban_parcel",
        pages=(page,),
        absent=("parcel:8/rules.parking_requirement",),
        stated={
            "parcel:8/rules.max_far": 1.4,
            "parcel:7/rules.parking_requirement": "1 PM na 1 stan, u garaži u suterenu",
        },
        faithful=response(nf()),
        guessing=response(v("1 PM na 1 stan", parking, page=9)),
    )


_TITLE = """Glavni grad Podgorica
Izmjene i dopune Detaljnog urbanističkog plana „Blok 5" u Podgorici
Obrađivač: Urbanistički zavod d.o.o.
Plan je objavljen u „Službenom listu Crne Gore – opštinski propisi", broj 35/12.
Površina obuhvata: 12,4 ha
Podgorica, novembar 2012. godine"""


def _case_adoption_date() -> AbsentCase:
    page = text_page(1, _TITLE)
    title = 'Izmjene i dopune Detaljnog urbanističkog plana „Blok 5" u Podgorici'

    def response(date: dict[str, Any]) -> dict[str, Any]:
        return document(
            name=v(title, page=1),
            document_type=v("DUP", "Detaljnog urbanističkog plana", page=1),
            status=v(
                "adopted",
                'Plan je objavljen u „Službenom listu Crne Gore – opštinski propisi"',
                page=1,
            ),
            gazette_reference=v(
                '„Službenom listu Crne Gore – opštinski propisi", broj 35/12',
                page=1,
                confidence=0.9,
            ),
            area_ha=v("12,4 ha", "Površina obuhvata: 12,4 ha", page=1, unit="ha"),
            decision_date=date,
            amendments=[
                {
                    "relation": "amends",
                    "document_name": v('Detaljnog urbanističkog plana „Blok 5"', title, page=1),
                }
            ],
        )

    return AbsentCase(
        id="adoption_date_from_plan_date",
        trap="the plan's date (novembar 2012) and the gazette number 35/12 invite an adoption date",
        task="document",
        pages=(page,),
        absent=("document/decision_date", "document/decision_number"),
        stated={"document/area_ha": 12.4, "document/document_type": "DUP"},
        faithful=response(nf()),
        guessing=response(v("01.11.2012", "Podgorica, novembar 2012. godine", page=1)),
    )


ABSENT_CASES: tuple[AbsentCase, ...] = (
    _case_far(),
    _case_height(),
    _case_area(),
    _case_parking(),
    _case_adoption_date(),
)
