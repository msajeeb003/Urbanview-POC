"""The document seed list: the client's zone session starts from the reference, not a blank sheet.

:func:`build_seed` turns the manual copy of the reference page (``[reference] capture``:
``## <zone>`` sections, ``<type> | <document as listed>`` lines) into the two CSVs the QGIS
template loads: ``zones.csv`` (the listed areas with stable ids) and ``zone_documents.csv`` (one
row per listed document, in its zone). Every listed document is cross-checked against a snapshot
of the official registry (eRegistri, ``[eregistri] snapshot``; ``refresh`` makes the one list call
again), so the session only confirms, corrects and marks status:

- matched: the registry id, name, gazette and note travel with the row and the source URL is the
  registry's details page; a registry note carrying an invalid marker suggests ``superseded``;
- not matched: listed with a year -> ``adopted`` to confirm; listed without one -> ``in_progress``
  (the reference lists plans still in preparation without a year).

Nothing is invented: no adoption date (the registry has none), no status beyond those rules, and
every suggestion says why in ``notes``. Registry entries the reference does not list are reported
(the client may add them to a zone). Unadopted rows are recorded; coverage takes adopted ones only.

**Matching** (:func:`match_registry`). Both names are folded (:func:`core.zones.schema.name_key`),
split into tokens, roman numerals II-XX become digits ("Stambena zajednica VI" = "... 6"; a lone
"i" is the conjunction, never 1), the document-type abbreviations are dropped (derived from the
listed badges and the registry's type keys, never a fixed list) and every word is cut to a light
stem (trailing vowels off, at most 7 letters: "Dajbabska gora" = "Dajbabske gore"). Filler words
("izmjene i dopune", "dio" ...; ``[eregistri] filler_words`` overrides them) and the words of the
registry's type names ("Prostorno-urbanističkog plana") weigh :data:`FILLER_WEIGHT`, so they break
ties without deciding a match. The score is a weighted Dice coefficient on the token sets, where
two different words of 4+ letters count partially when their spelling is close (``difflib`` ratio
>= :data:`SOFT_TOKEN_MIN`: "Titeks" / "Titex", "Cvijećni" / "Cvijetin"). ``exact`` means the
normalised token sets are identical. Pairs whose badge and registry type disagree, or whose
numbers differ (numbers name the part of a plan: "Nova Varoš" is not "Nova Varoš 2", "Zabjelo 8"
not "Zabjelo 9"), are never considered. Assignment is greedy, best first, and no registry entry
is taken twice. Two bonuses order the ranking (never the reported score or the threshold): a
listed year equal to the entry's gazette year (:data:`YEAR_BONUS`: two amendments of one plan
with the same name go to the entries of their own years) and registry words that name the listed
zone (:data:`CONTEXT_BONUS`: "Stambena zajednica VI Stara Varoš" for the "Stambena zajednica 6"
listed under Stara Varoš).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Any, Literal

from core.extraction.textmatch import words_fold
from core.zones.config import EregistriConfig, ZoneSetConfig
from core.zones.schema import (
    DOCUMENT_FIELD_NAMES,
    SLUG_RE,
    ZONE_FIELD_NAMES,
    document_identity,
    name_key,
    parse_bool,
    slugify,
    split_year,
)

# Montenegrin connective words of plan titles; ``[eregistri] filler_words`` in zones.toml replaces
# them for a municipality whose documents are written otherwise.
FILLER_WORDS: tuple[str, ...] = (
    "izmjene",
    "izmjena",
    "dopune",
    "dopuna",
    "i",
    "dio",
    "dijela",
    "za",
    "sa",
    "u",
    "na",
    "od",
    "do",
)
FILLER_WEIGHT = 0.1  # a filler token counts a tenth of a name word
SOFT_TOKEN_MIN = 0.7  # difflib ratio from which two different words count (partially) as one
YEAR_BONUS = 0.05  # ranking bonus when the listed year equals the entry's gazette year
CONTEXT_BONUS = 0.05  # ranking bonus when the entry's extra words name the listed zone
STEM_LENGTH = 7
USER_AGENT = "UrbanView zone tooling (one registry list request per seed run)"
REQUEST_TIMEOUT_SECONDS = 60.0

MatchMethod = Literal["exact", "fuzzy"]


# --- the reference listing -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListedDocument:
    zone_name: str
    badge: str  # the type badge as listed ("DUP")
    listing: str  # the document text as listed ("DUP Momišići C (2009)")
    name: str  # the listing without its "(yyyy)" suffix
    year: int | None
    line: int  # 1-based line of the capture file


@dataclass(slots=True)
class ListedZone:
    name: str
    documents: list[ListedDocument] = field(default_factory=list)
    line: int = 0


def parse_capture(path: Path) -> list[ListedZone]:
    """The reference structure from its manual copy: ``## <zone>`` starts a zone, each following
    ``<type> | <document>`` line is one of its documents; ``#`` comments and blank lines are
    skipped. Anything else is a ValueError naming the line (a silently dropped document would
    reach the client's session as a document that does not exist)."""
    path = Path(path)
    zones: list[ListedZone] = []
    seen: set[str] = set()
    current: ListedZone | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = " ".join(raw.split())
        if not line:
            continue
        if line.startswith("##"):
            name = line[2:].strip()
            if not name:
                raise ValueError(f"{path}:{number}: zone header without a name")
            if name in seen:
                raise ValueError(f"{path}:{number}: zone {name!r} is listed twice")
            seen.add(name)
            current = ListedZone(name=name, line=number)
            zones.append(current)
            continue
        if line.startswith("#"):
            continue
        if current is None:
            raise ValueError(f"{path}:{number}: document before the first '## <zone>' header")
        badge, sep, listing = (part.strip() for part in line.partition("|"))
        if not sep or not badge or not listing:
            raise ValueError(f"{path}:{number}: expected '<type> | <document>', got {line!r}")
        name, year = split_year(listing)
        current.documents.append(
            ListedDocument(
                zone_name=current.name,
                badge=badge,
                listing=listing,
                name=name,
                year=year,
                line=number,
            )
        )
    return zones


# --- the registry snapshot -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    id: str
    code: str
    name: str
    type_name: str
    type_key: str | None  # the profile's document type key, None when the type is not mapped
    gazette: str
    gazette_year: int | None
    registered_on: str  # as the registry prints it (registration, not adoption)
    area: str
    cadastral_municipalities: tuple[str, ...]
    note: str
    invalid: bool
    invalid_marker: str | None = None  # the configured marker found in the note


_GAZETTE_NUMBER = re.compile(r"(?<!\d)\d{1,3}\s*/\s*(\d{4}|\d{2})(?!\d)")
_FOUR_DIGIT_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def gazette_year(text: str) -> int | None:
    """The year of a gazette reference: "Sl. list CG br. 64/08" -> 2008, "br.46/01" -> 2001,
    "broj 6/2014" -> 2014; the first "number/year" wins over later dates ("br. 032/18 od
    18.09.2018"). Two-digit years are this century unless that would be in the future."""
    match = _GAZETTE_NUMBER.search(text or "")
    if match:
        digits = match.group(1)
        if len(digits) == 4:
            return int(digits)
        year = 2000 + int(digits)
        return year if year <= date.today().year + 1 else year - 100
    match = _FOUR_DIGIT_YEAR.search(text or "")
    return int(match.group(0)) if match else None


def invalid_marker(note: str, markers: Iterable[str]) -> str | None:
    """The first marker the note contains, compared accent-folded and case-insensitive."""
    folded = words_fold(note or "")
    for marker in markers:
        if marker.strip() and words_fold(marker) in folded:
            return marker
    return None


def parse_registry(data: Mapping[str, Any], cfg: EregistriConfig) -> list[RegistryEntry]:
    """Registry entries from the jqGrid list response (``rows[].id``, ``rows[].cell[]`` with the
    configured column indexes). A partial list (fewer rows than ``records``) is refused: missing
    entries would turn into wrong "not in eRegistri" suggestions."""
    rows = data.get("rows") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("not an eRegistri list response: no 'rows' array")
    records = data.get("records")
    if isinstance(records, int) and records > len(rows):
        raise ValueError(
            f"the registry list has {len(rows)} of {records} records: raise rows= in list_url"
        )
    types = {words_fold(name): key for name, key in cfg.types.items()}

    def cell(cells: Sequence[Any], key: str) -> str:
        index = cfg.columns.get(key)
        if index is None or not -len(cells) <= index < len(cells):
            return ""
        value = cells[index]
        return "" if value is None else str(value).strip()

    entries: list[RegistryEntry] = []
    for row in rows:
        cells = row.get("cell") or []
        type_name = " ".join(cell(cells, "type").split())
        gazette = " ".join(cell(cells, "gazette").split())
        note = cell(cells, "note")
        marker = invalid_marker(note, cfg.invalid_markers)
        kos = cell(cells, "cadastral_municipalities")
        entries.append(
            RegistryEntry(
                id=str(row.get("id", "")).strip(),
                code=cell(cells, "code"),
                name=" ".join(cell(cells, "name").split()),
                type_name=type_name,
                type_key=types.get(words_fold(type_name)),
                gazette=gazette,
                gazette_year=gazette_year(gazette),
                registered_on=cell(cells, "registered_on"),
                area=cell(cells, "area"),
                cadastral_municipalities=tuple(k.strip() for k in kos.split(",") if k.strip()),
                note=note,
                invalid=marker is not None,
                invalid_marker=marker,
            )
        )
    return entries


def load_registry(
    cfg: EregistriConfig,
    *,
    refresh: bool = False,
    get: Callable[..., Any] | None = None,
) -> list[RegistryEntry]:
    """The registry entries of the snapshot. ``refresh`` first makes ONE GET of the list URL
    (``get`` defaults to ``httpx.get``; tests inject one returning a response-like object) and
    replaces the snapshot with it, so a seed run is repeatable offline and says what it saw."""
    snapshot = Path(cfg.snapshot)
    if refresh:
        if not cfg.list_url:
            raise ValueError("no [eregistri] list_url configured: cannot refresh the snapshot")
        if get is None:
            import httpx  # imported here: the offline seed needs no HTTP client

            get = httpx.get
        response = get(
            cfg.list_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError(f"eRegistri answered something other than JSON: {exc}") from exc
        entries = parse_registry(data, cfg)  # validate before the snapshot is replaced
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot.with_name(snapshot.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, snapshot)
        return entries
    if not snapshot.is_file():
        raise FileNotFoundError(
            f"no eRegistri snapshot at {snapshot}: run the seed with --refresh-eregistri once"
        )
    return parse_registry(json.loads(snapshot.read_text(encoding="utf-8-sig")), cfg)


# --- matching ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryMatch:
    entry: RegistryEntry
    score: float
    method: MatchMethod


def _roman(n: int) -> str:
    out = ""
    for value, symbol in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += symbol
            n -= value
    return out


# II..XX only: a lone "i" is the conjunction ("Novi Grad 1 i 2"), never the numeral one
_ROMAN = {_roman(n): str(n) for n in range(2, 21)}
_TOKEN = re.compile(r"[^\W_]+")


def _stem(token: str) -> str:
    if not token.isalpha() or len(token) <= 3:
        return token
    while len(token) > 3 and token[-1] in "aeiou":
        token = token[:-1]
    return token[:STEM_LENGTH]


def name_tokens(name: str, drop: Iterable[str] = ()) -> list[str]:
    """The comparable tokens of a plan name (see the module docstring), in order."""
    dropped = set(drop)
    out: list[str] = []
    for raw in _TOKEN.findall(name_key(name)):
        if raw in dropped:
            continue
        out.append(_stem(_ROMAN.get(raw, raw)))
    return out


@dataclass(frozen=True, slots=True)
class _Name:
    weights: dict[str, float]
    total: float
    numbers: frozenset[str]  # "Blok 5", "Zabjelo 8", "faza III": the part of a plan


def _profile(name: str, drop: frozenset[str], low: frozenset[str]) -> _Name:
    weights = {t: (FILLER_WEIGHT if t in low else 1.0) for t in name_tokens(name, drop)}
    numbers = frozenset(t for t in weights if t.isdigit())
    return _Name(weights, sum(weights.values()), numbers)


def _softable(token: str) -> bool:
    return token.isalpha() and len(token) >= 4


@cache
def _spelling(x: str, y: str) -> float:
    return SequenceMatcher(None, x, y).ratio()


def _similarity(a: _Name, b: _Name, floor: float = 0.0) -> float:
    """Weighted Dice coefficient; close spellings of two leftover words count by their ratio.
    Returns 0 early when even a perfect spelling match of the leftovers stays below ``floor``."""
    if not a.total or not b.total:
        return 0.0
    common = sum(min(w, b.weights[t]) for t, w in a.weights.items() if t in b.weights)
    rest_a = [t for t in a.weights if t not in b.weights and _softable(t)]
    rest_b = [t for t in b.weights if t not in a.weights and _softable(t)]
    best = common + min(sum(a.weights[t] for t in rest_a), sum(b.weights[t] for t in rest_b))
    if 2 * best / (a.total + b.total) < floor:
        return 0.0
    if rest_a and rest_b:
        pairs = sorted(((_spelling(x, y), x, y) for x in rest_a for y in rest_b), reverse=True)
        used_a: set[str] = set()
        used_b: set[str] = set()
        for ratio, x, y in pairs:
            if ratio < SOFT_TOKEN_MIN:
                break
            if x in used_a or y in used_b:
                continue
            used_a.add(x)
            used_b.add(y)
            common += ratio * min(a.weights[x], b.weights[y])
    return round(2 * common / (a.total + b.total), 4)


def match_registry(
    documents: Sequence[ListedDocument],
    entries: Sequence[RegistryEntry],
    *,
    threshold: float,
    filler: Iterable[str] | None = None,
) -> list[RegistryMatch | None]:
    """One registry match (or None) per listed document, in the documents' order. Greedy best
    first over every pair scoring at least ``threshold``; no entry is taken twice."""
    drop = frozenset(
        {e.type_key.casefold() for e in entries if e.type_key}
        | {name_key(d.badge) for d in documents if d.badge}
    )
    low_words = list(filler if filler is not None else FILLER_WORDS)
    low_words += [e.type_name for e in entries if e.type_name]  # "Prostorno urbanistički plan"
    low = frozenset(t for word in low_words for t in name_tokens(word, drop))
    doc_names = [_profile(d.name, drop, low) for d in documents]
    entry_names = [_profile(e.name, drop, low) for e in entries]
    zone_words = {
        zone: {t for t in name_tokens(zone, drop) if t not in low}
        for zone in {d.zone_name for d in documents}
    }

    candidates: list[tuple[float, int, int, int, float, bool]] = []
    for i, doc in enumerate(documents):
        badge = doc.badge.strip().upper()
        mine = doc_names[i]
        for j, entry in enumerate(entries):
            theirs = entry_names[j]
            if badge and entry.type_key and badge != entry.type_key.upper():
                continue
            if mine.numbers != theirs.numbers:  # "Nova Varoš" is not "Nova Varoš 2"
                continue
            score = _similarity(mine, theirs, threshold)
            if score < threshold:
                continue
            both = doc.year is not None and entry.gazette_year is not None
            gap = abs(doc.year - entry.gazette_year) if both else 9999  # type: ignore[operator]
            rank = score + (YEAR_BONUS if both and gap == 0 else 0.0)
            # the registry naming the listed area ("Stambena zajednica VI Stara Varoš" for the
            # "Stambena zajednica 6" listed under Stara Varoš) is the same kind of evidence
            extra = {t for t, w in theirs.weights.items() if w == 1.0 and t not in mine.weights}
            if extra & zone_words[doc.zone_name]:
                rank += CONTEXT_BONUS
            exact = mine.weights.keys() == theirs.weights.keys()
            candidates.append((-rank, gap, i, j, score, exact))
    candidates.sort()

    result: list[RegistryMatch | None] = [None] * len(documents)
    taken: set[int] = set()
    for _, _, i, j, score, exact in candidates:
        if result[i] is not None or j in taken:
            continue
        result[i] = RegistryMatch(entries[j], score, "exact" if exact else "fuzzy")
        taken.add(j)
    return result


# --- the seed ------------------------------------------------------------------------------


@dataclass(slots=True)
class SeedResult:
    zones: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    stats: dict[str, int]
    warnings: list[str]
    unlisted: list[RegistryEntry] = field(default_factory=list)  # in eRegistri, not listed


def _filler(cfg: ZoneSetConfig) -> tuple[str, ...] | None:
    words = (cfg.raw.get("eregistri") or {}).get("filler_words")
    return tuple(words) if words is not None else None


def _document_types(municipality_id: str) -> set[str]:
    from core.municipality import load_profile

    return {key.upper() for key in load_profile(municipality_id).terminology.document_types}


def _zone_id(cfg: ZoneSetConfig, name: str) -> str:
    zone_id = cfg.reference.zone_ids.get(name) or slugify(name)
    if not SLUG_RE.match(zone_id):
        raise ValueError(f"zone {name!r}: {zone_id!r} is not a valid zone id (a lower-case slug)")
    return zone_id


def _document_row(
    zone_id: str, doc: ListedDocument, match: RegistryMatch | None, details: Callable[[str], str]
) -> dict[str, Any]:
    notes: list[str] = []
    entry = match.entry if match else None
    if entry is None:
        if doc.year is not None:
            status = "adopted"
            notes.append("not found in eRegistri: confirm")
        else:
            status = "in_progress"
            notes.append(
                "listed without a year and not in eRegistri: likely in preparation; confirm"
            )
    else:
        if entry.invalid:
            status = "superseded"
            notes.append(f'eRegistri marks it invalid ("{entry.invalid_marker}")')
        else:
            status = "adopted"
        if doc.year is None:
            notes.append(
                f"listed without a year (the reference may mean a plan in preparation); "
                f"eRegistri {entry.gazette or 'has no gazette'}: confirm"
            )
        elif entry.gazette_year is not None and entry.gazette_year != doc.year:
            notes.append(
                f"listed {doc.year}, eRegistri gazette {entry.gazette_year} ({entry.gazette}): "
                "confirm which version applies"
            )
        if entry.type_key is None:
            notes.append(f"eRegistri type {entry.type_name!r} is not mapped to a document type")
        elif entry.type_key.upper() != doc.badge.upper():
            notes.append(f"eRegistri type {entry.type_key} ({entry.type_name}), listed {doc.badge}")
    return {
        "zone_id": zone_id,
        "document_name": doc.name,
        "document_type": doc.badge,
        "status": status,
        "eregistri_reference": entry.id if entry else "",
        "source_url": details(entry.id) if entry else "",
        "adoption_date": "",  # the registry has no adoption dates; never invent one
        "notes": "; ".join(notes),
        "poc_coverage": False,
        "confirmed": False,
        "listed_as": doc.listing,
        "listed_year": doc.year,
        "eregistri_name": entry.name if entry else "",
        "eregistri_code": entry.code if entry else "",
        "eregistri_gazette": entry.gazette if entry else "",
        "eregistri_note": " ".join(entry.note.split()) if entry else "",
        "match": "none" if match is None else _match_label(match),
    }


def _match_label(match: RegistryMatch) -> str:
    return "exact" if match.method == "exact" else f"fuzzy:{match.score:.2f}"


def build_seed(
    cfg: ZoneSetConfig,
    *,
    refresh_eregistri: bool = False,
    get: Callable[..., Any] | None = None,
) -> SeedResult:
    """The zone list and the document seed list (rows in ``ZONE_FIELD_NAMES`` /
    ``DOCUMENT_FIELD_NAMES`` order) from the reference capture and the registry snapshot."""
    listed = parse_capture(cfg.reference.capture)
    entries = load_registry(cfg.eregistri, refresh=refresh_eregistri, get=get)
    doc_types = _document_types(cfg.municipality_id)
    warnings: list[str] = []

    zone_ids: dict[str, str] = {}
    for zone in listed:
        zone_id = _zone_id(cfg, zone.name)
        if zone_id in zone_ids.values():
            raise ValueError(f"zone {zone.name!r}: zone id {zone_id!r} is used twice")
        if zone.name not in cfg.reference.zone_ids:
            warnings.append(
                f"zone {zone.name!r} has no [reference.zone_ids] entry: id {zone_id!r} derived "
                "from its name (add it to zones.toml to keep it stable)"
            )
        zone_ids[zone.name] = zone_id

    documents = [doc for zone in listed for doc in zone.documents]
    matches = match_registry(
        documents, entries, threshold=cfg.eregistri.match_threshold, filler=_filler(cfg)
    )
    capture = cfg.reference.capture.name
    rows: list[dict[str, Any]] = []
    for doc, match in zip(documents, matches, strict=True):
        if doc.badge.upper() not in doc_types:
            warnings.append(
                f"{capture}:{doc.line}: type {doc.badge!r} is not a document type of the "
                f"{cfg.municipality_id} profile (kept as listed)"
            )
        rows.append(_document_row(zone_ids[doc.zone_name], doc, match, cfg.eregistri.details))

    identities = Counter(document_identity(row) for row in rows)
    for row, doc in zip(rows, documents, strict=True):
        if identities[document_identity(row)] > 1:
            warnings.append(
                f"{capture}:{doc.line}: {doc.listing!r} is listed more than once "
                "(a document belongs to exactly one zone)"
            )

    zone_rows = [
        {
            "zone_id": zone_ids[zone.name],
            "name": zone.name,
            "zone_type": "",
            "general_planning_summary": "",
            "notes": f"Listed on {cfg.reference.name or 'the reference'}",
            "no_adopted_plan": False,
        }
        for zone in listed
    ]
    for zone in zone_rows:
        if not any(r["zone_id"] == zone["zone_id"] and r["status"] == "adopted" for r in rows):
            warnings.append(
                f"zone {zone['zone_id']}: no document suggested as adopted; set no_adopted_plan "
                "when the client confirms it has none"
            )

    matched = {m.entry.id for m in matches if m is not None}
    unlisted = [e for e in entries if e.id not in matched]
    for entry in unlisted:
        flag = " [marked invalid]" if entry.invalid else ""
        warnings.append(
            f"eRegistri only: {entry.code} {entry.name!r} ({entry.type_key or entry.type_name}, "
            f"{entry.gazette or 'no gazette'}){flag}: not listed on the reference; add it to a "
            "zone if it applies"
        )

    status = Counter(row["status"] for row in rows)
    stats = {
        "zones": len(zone_rows),
        "documents": len(rows),
        "matched_exact": sum(1 for m in matches if m is not None and m.method == "exact"),
        "matched_fuzzy": sum(1 for m in matches if m is not None and m.method == "fuzzy"),
        "unmatched": sum(1 for m in matches if m is None),
        "suggested_adopted": status["adopted"],
        "suggested_in_progress": status["in_progress"],
        "suggested_superseded": status["superseded"],
        "year_mismatch": sum(
            1
            for doc, m in zip(documents, matches, strict=True)
            if m is not None
            and doc.year is not None
            and m.entry.gazette_year is not None
            and m.entry.gazette_year != doc.year
        ),
        "registry_entries": len(entries),
        "registry_unlisted": len(unlisted),
    }
    return SeedResult(zone_rows, rows, stats, warnings, unlisted)


# --- CSV files -----------------------------------------------------------------------------


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # utf-8-sig: Excel opens č ć š ž đ correctly only with the byte-order mark
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _csv_value(row.get(name)) for name in fieldnames})
    os.replace(tmp, path)
    return path


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Rows of a seed / export CSV as stripped strings. The delimiter is taken from the header
    (",", ";" or tab: Excel in a Montenegrin locale saves with ";"); blank rows are skipped."""
    text = Path(path).read_text(encoding="utf-8-sig")
    header = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=header.count) if header.strip() else ","
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter):
        clean = {
            key.strip(): (value.strip() if isinstance(value, str) else "")
            for key, value in row.items()
            if key is not None
        }
        if any(clean.values()):
            rows.append(clean)
    return rows


def _is_confirmed(value: str | None) -> bool:
    try:
        return parse_bool(value) is True
    except ValueError:
        return True  # an unreadable flag might be the client's "yes": do not overwrite it


def write_seed(cfg: ZoneSetConfig, result: SeedResult, *, overwrite: bool = False) -> list[Path]:
    """Writes the zone list and the document seed list. An existing document list with any
    confirmed row is the client's work: refused (FileExistsError) unless ``overwrite``; the check
    runs before anything is written."""
    if not overwrite and cfg.documents.is_file():
        confirmed = sum(
            1 for row in read_csv_rows(cfg.documents) if _is_confirmed(row.get("confirmed"))
        )
        if confirmed:
            raise FileExistsError(
                f"{cfg.documents} has {confirmed} confirmed row(s): writing the seed would "
                "discard the client's confirmations; overwrite it explicitly (--force) if intended"
            )
    return [
        _write_csv(cfg.zones_seed, ZONE_FIELD_NAMES, result.zones),
        _write_csv(cfg.documents, DOCUMENT_FIELD_NAMES, result.documents),
    ]
