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
MARKET_PARAMETER_LABELS: dict[str, Bilingual] = {
    "land_rate_eur_m2": Bilingual("Land value per m²", "Vrijednost zemljišta po m²"),
    "build_rate_eur_m2": Bilingual("Construction cost per m² GFA", "Cijena gradnje po m² BGP"),
    "design_rate_eur_m2": Bilingual(
        "Design & documentation per m² GFA", "Projektovanje i dokumentacija po m² BGP"
    ),
    "sale_rate_eur_m2": Bilingual("Selling price per m²", "Prodajna cijena po m²"),
}
ZONE_TYPICAL_NOTE = Bilingual(
    "Typical values for the zone, maintained by staff; the values of a parcel's own planning "
    "document always take precedence.",
    "Tipične vrijednosti za zonu koje održava osoblje; vrijednosti iz planskog dokumenta "
    "parcele uvijek imaju prednost.",
)

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


# --- display-shaped panels (GET /v1/parcels/{id}/panel, GET /v1/zones/{id}/panel) ------------

PANEL_SECTIONS: dict[str, Bilingual] = {
    "group1": Bilingual("Planning parameters", "Planski parametri"),
    "market": Bilingual("Market data", "Tržišni podaci"),
    "assumptions": Bilingual("Assumptions", "Pretpostavke"),
    "group2": Bilingual("Financial feasibility", "Finansijska izvodljivost"),
}
ZONE_SUMMARY_LABEL = Bilingual("General planning summary", "Opšti planski sažetak")

# Why a Group 1 value is null (never a default).
FIELD_GAP_REASONS: dict[str, Bilingual] = {
    "not_in_document": Bilingual(
        "not stated in the planning document", "nije navedeno u planskom dokumentu"
    ),
    "rejected": Bilingual(
        "the extracted value was rejected in expert review and is not shown",
        "izvučena vrijednost je odbijena u stručnoj provjeri i nije prikazana",
    ),
    "unpublished": Bilingual(
        "planning data has not been published yet", "planski podaci još nisu objavljeni"
    ),
}


def field_gap_reason(code: str) -> Bilingual:
    return FIELD_GAP_REASONS[code]


# Which area the calculations use and why (ParcelHeader.calculation_basis).
PARCEL_BASIS_EXPLANATIONS: dict[str, Bilingual] = {
    "planned_parcel": Bilingual(
        "Calculations use planned urban parcel {urban_parcel_number} ({planned_m2} m²), which "
        "covers {overlap_pct}% of this cadastral parcel ({cadastral_m2} m²): the adopted plan "
        "defines the parcel that can be built on.",
        "Proračuni koriste urbanističku parcelu {urban_parcel_number} ({planned_m2} m²), koja "
        "pokriva {overlap_pct}% ove katastarske parcele ({cadastral_m2} m²): usvojeni plan "
        "definiše parcelu na kojoj se gradi.",
    ),
    "split": Bilingual(
        "This cadastral parcel lies in {count} planned urban parcels ({numbers}). Calculations use "
        "{urban_parcel_number} ({planned_m2} m²), which covers the largest part ({overlap_pct}%); "
        "the others are listed and have their own figures.",
        "Ova katastarska parcela leži u više urbanističkih parcela ({count}: {numbers}). "
        "Proračuni koriste {urban_parcel_number} ({planned_m2} m²), koja pokriva najveći dio "
        "({overlap_pct}%); ostale su navedene i imaju svoje podatke.",
    ),
    "no_planned_parcel": Bilingual(
        "No planned urban parcel is defined over this cadastral parcel; calculations use the "
        "cadastral area ({cadastral_m2} m²) with the general values of {document_name}.",
        "Nad ovom katastarskom parcelom nije definisana urbanistička parcela; proračuni koriste "
        "katastarsku površinu ({cadastral_m2} m²) i opšte vrijednosti dokumenta {document_name}.",
    ),
    "not_covered": Bilingual(
        "No adopted planning document covers this parcel, so there is no calculation basis.",
        "Nijedan usvojeni planski dokument ne pokriva ovu parcelu, pa nema osnove za proračun.",
    ),
    "unpublished": Bilingual(
        "Planning data has not been published yet; the calculation basis is set at the first "
        "publish.",
        "Planski podaci još nisu objavljeni; osnova za proračun određuje se prvim objavljivanjem.",
    ),
}


def parcel_basis_text(code: str, params: dict[str, Any] | None) -> Bilingual:
    return PARCEL_BASIS_EXPLANATIONS[code].format(**_text_params(params))


AREA_MISMATCH_NOTE = Bilingual(
    "Planned area {planned_m2} m² vs cadastral area {cadastral_m2} m²: difference {delta_m2} m² "
    "({delta_pct}%).",
    "Planirana površina {planned_m2} m² naspram katastarske {cadastral_m2} m²: razlika "
    "{delta_m2} m² ({delta_pct}%).",
)


def area_mismatch_text(**params: Any) -> Bilingual:
    return AREA_MISMATCH_NOTE.format(**_text_params(params))


PARCEL_FLAG_LABELS: dict[str, Bilingual] = {
    "public_ownership": Bilingual("Public ownership", "Javna svojina"),
    "restitution_or_legal_burden": Bilingual(
        "Restitution or legal burden", "Restitucija ili pravni teret"
    ),
}

# The effective assumptions (AssumptionsView.items); unit "share" = 0–1 shown as a percentage.
ASSUMPTION_LABELS: dict[str, FeasibilityLabel] = {
    "construction_cost_eur_m2": FeasibilityLabel(
        "Construction cost per m² GFA", "Cijena gradnje po m² BGP", "€/m²"
    ),
    "saleable_share": FeasibilityLabel("Saleable share of GFA", "Prodajni udio BGP", "share"),
    "sale_price_eur_m2": FeasibilityLabel("Selling price per m²", "Prodajna cijena po m²", "€/m²"),
    "land_value_eur_m2": FeasibilityLabel(
        "Land value per m² of parcel", "Vrijednost zemljišta po m² parcele", "€/m²"
    ),
    "design_documentation_eur_m2": FeasibilityLabel(
        "Design & documentation per m² GFA", "Projektovanje i dokumentacija po m² BGP", "€/m²"
    ),
}

# Group 2 input flags: what a missing Group 1 input means for the figures.
INPUT_FLAG_NOTES: dict[str, Bilingual] = {
    "max_far": Bilingual(
        "Without the FAR the gross floor area and every figure derived from it cannot be "
        "calculated.",
        "Bez indeksa izgrađenosti ne može se izračunati BGP ni iznosi koji iz nje proizlaze.",
    ),
    "max_site_coverage_pct": Bilingual(
        "Without the site coverage only the max coverage area is missing; the financial figures "
        "are unaffected.",
        "Bez indeksa zauzetosti nedostaje samo maksimalna površina pod objektom; finansijski "
        "iznosi nisu pogođeni.",
    ),
    "max_height_m": Bilingual(
        "Not an input of the formulas: the figures assume the full FAR can be built within the "
        "plan's height rules; check the plan.",
        "Nije ulaz formula: iznosi pretpostavljaju da se puni indeks izgrađenosti može ostvariti "
        "u okviru visinskih pravila plana; provjerite plan.",
    ),
    "max_floors": Bilingual(
        "Not an input of the formulas: the figures assume the full FAR can be built within the "
        "plan's floor limits; check the plan.",
        "Nije ulaz formula: iznosi pretpostavljaju da se puni indeks izgrađenosti može ostvariti "
        "u okviru dozvoljene spratnosti; provjerite plan.",
    ),
}
