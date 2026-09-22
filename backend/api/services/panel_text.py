"""Every user-facing string of the information panel (``docs/specs/panel-payload.md`` section 5).

The engine and the SQL emit codes and numbers only; this module turns them into bilingual text
(``en`` / ``me``). Montenegrin wording is provisional until the client validates it (P0 gate 3)
and the disclaimer awaits the lawyer, hence ``DISCLAIMER_STATUS = "placeholder"``. Nothing here
knows a municipality: labels come from the field dictionary or from these product-wide constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Bilingual:
    en: str
    me: str

    def format(self, **params: Any) -> Bilingual:
        return Bilingual(en=self.en.format(**params), me=self.me.format(**params))


# --- document status (DocumentRef.status_label_en / _me) ---------------------------------------

DOCUMENT_STATUS_LABELS: dict[str, Bilingual] = {
    "adopted": Bilingual("adopted", "usvojen"),
    "in_progress": Bilingual("in progress", "u izradi"),
    "superseded": Bilingual("superseded", "zamijenjen"),
}


def document_status_label(status: str) -> Bilingual:
    """Unknown statuses cannot come from the enum column; fall back to the raw value anyway."""
    return DOCUMENT_STATUS_LABELS.get(status, Bilingual(status.replace("_", " "), status))


# --- fixed labels --------------------------------------------------------------------------------

ZONE_SUBTITLE = Bilingual("Internal city division", "Interna podjela grada")

FLAGS_NOTE = Bilingual(
    "false means not flagged in the cadastral extract",
    "false znači da nije označeno u katastarskom izvodu",
)

NOT_STATED_LABEL = Bilingual("not stated in plan", "nije navedeno u planu")

# Neutral coverage notes (CLAUDE.md: uncovered locations are not errors, never a red state).
CADASTRAL_NOT_COVERED_NOTE = Bilingual(
    "No adopted planning document covers this parcel; planning parameters and feasibility are "
    "shown for adopted documents only.",
    "Nijedan usvojeni planski dokument ne pokriva ovu parcelu; planski parametri i izvodljivost "
    "prikazuju se samo za usvojene dokumente.",
)
URBAN_NOT_COVERED_NOTE = Bilingual(
    "Planning document {document_name} is {status_en}; planning parameters and feasibility are "
    "shown for adopted documents only.",
    "Planski dokument {document_name} je {status_me}; planski parametri i izvodljivost "
    "prikazuju se samo za usvojene dokumente.",
)

# --- calculation basis (Areas.basis_reason_*) ---------------------------------------------------

BASIS_REASONS: dict[str, Bilingual] = {
    "urban_covers_cadastral": Bilingual(
        "planned parcel {urban_parcel_number} covers {share_pct}% of cadastral parcel "
        "{cadastral_parcel_number}",
        "urbanistička parcela {urban_parcel_number} pokriva {share_pct}% katastarske parcele "
        "{cadastral_parcel_number}",
    ),
    "no_urban_parcel": Bilingual(
        "no planned parcel is defined over cadastral parcel {cadastral_parcel_number}; "
        "cadastral area used",
        "nad katastarskom parcelom {cadastral_parcel_number} nije definisana urbanistička "
        "parcela; koristi se katastarska površina",
    ),
    "no_cadastral_parcel": Bilingual(
        "planned parcel {urban_parcel_number} has no cadastral parcel under it",
        "za urbanističku parcelu {urban_parcel_number} nije evidentirana katastarska parcela",
    ),
}


def _text_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Numbers render without a spurious ``.0`` ("covers 70% of", "covers 56.3% of")."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if isinstance(value, float) and value.is_integer():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def basis_reason_text(code: str, params: dict[str, Any] | None) -> Bilingual:
    return BASIS_REASONS[code].format(**_text_params(params))


# --- engine reason codes (FeasibilityField / computed PlanningField reason_en / _me) -----------

REASON_TEXTS: dict[str, Bilingual] = {
    "area_unknown": Bilingual("parcel area unknown", "površina parcele nije poznata"),
    "far_not_stated": Bilingual(
        "FAR not stated in plan", "indeks izgrađenosti nije naveden u planu"
    ),
    "coverage_not_stated": Bilingual(
        "site coverage not stated in plan", "indeks zauzetosti nije naveden u planu"
    ),
    "requires_gfa": Bilingual("requires max GFA", "zahtijeva maksimalnu BGP"),
    "no_market_data": Bilingual(
        "no market data for zone {zone_name}", "nema tržišnih podataka za zonu {zone_name}"
    ),
    "no_market_data_zone_unknown": Bilingual(
        "no market data (zone unknown)", "nema tržišnih podataka (zona nepoznata)"
    ),
    "total_cost_zero": Bilingual("total cost is zero", "ukupni trošak je nula"),
}


def reason_text(code: str | None, params: dict[str, Any] | None) -> Bilingual | None:
    if code is None:
        return None
    return REASON_TEXTS[code].format(**_text_params(params))


# --- feasibility rows (FeasibilityField.label_en / label_me / unit) ----------------------------


@dataclass(frozen=True, slots=True)
class FeasibilityLabel:
    en: str
    me: str
    unit: str


FEASIBILITY_LABELS: dict[str, FeasibilityLabel] = {
    "max_gfa_m2": FeasibilityLabel("Max gross floor area", "Maksimalna BGP", "m²"),
    "max_coverage_area_m2": FeasibilityLabel(
        "Max coverage area", "Maksimalna površina pod objektom", "m²"
    ),
    "saleable_area_m2": FeasibilityLabel("Saleable area", "Prodajna površina", "m²"),
    "construction_cost_eur": FeasibilityLabel("Construction cost", "Troškovi izgradnje", "€"),
    "revenue_eur": FeasibilityLabel("Market value (revenue)", "Tržišna vrijednost", "€"),
    "profit_eur": FeasibilityLabel("Potential profit", "Potencijalna dobit", "€"),
    "roi_pct": FeasibilityLabel("Return on investment", "Povrat investicije", "%"),
    "land_value_eur": FeasibilityLabel(
        "Estimated land value", "Procijenjena vrijednost zemljišta", "€"
    ),
    "design_documentation_eur": FeasibilityLabel(
        "Design and documentation costs", "Troškovi projektovanja i dokumentacije", "€"
    ),
    "total_cost_eur": FeasibilityLabel("Total cost", "Ukupni trošak", "€"),
}


def feasibility_label(key: str) -> FeasibilityLabel:
    return FEASIBILITY_LABELS[key]


# --- disclaimer (FeasibilityBlock) --------------------------------------------------------------

DISCLAIMER = Bilingual(
    "Figures are indicative ranges derived from the adopted plan and public market data, not "
    "investment, planning or legal advice.",
    "Iznosi su indikativni rasponi izvedeni iz usvojenog plana i javnih tržišnih podataka, a ne "
    "investicioni, planerski ni pravni savjet.",
)
DISCLAIMER_STATUS = "placeholder"  # "client_approved" once the lawyer signs the wording off
DISCLAIMER_VERSION = "poc-1"
