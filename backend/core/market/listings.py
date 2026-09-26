"""Portal listings (Realitica, Estitor): the adapter seam and the manual paste import.

Scraping the portals is deferred to the pilot (BRD §2.4 lists them as sources; their terms and a
stable access method are open). What exists is the seam, :class:`ListingSource`, and one
implementation, :class:`PastedListings`: staff paste listings they looked up, one per line,

    location, asking price per m², date

(tab, ``;`` or ``|`` separated, or commas with the last two fields being the price and the
date; a first line of column names and ``#`` comment lines are skipped). A portal adapter later
implements the same ``fetch()`` and the rest of the path (placing listings in zones, the
per-zone median and quartiles, review) stays as it is. Asking prices are not transaction prices:
every figure made from them is flagged ``asking_prices`` for the reviewer.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Protocol

from core.market.model import Skipped
from core.market.parse import cell_text, parse_amount, parse_date, parse_unit

PORTAL_ADAPTERS: dict[str, type] = {}  # portal id -> ListingSource class; none until the pilot


@dataclass(frozen=True, slots=True)
class Listing:
    line: int
    location: str
    price_eur_m2: float
    listed_on: date
    raw: str

    def to_json(self) -> dict:
        data = asdict(self)
        data["listed_on"] = self.listed_on.isoformat()
        return data


@dataclass(slots=True)
class ListingBatch:
    listings: list[Listing]
    skipped: list[Skipped] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256("\n".join(self.lines).encode("utf-8")).hexdigest()


class ListingSource(Protocol):
    """Where listings come from: pasted text today, a portal adapter in the pilot."""

    name: str

    def fetch(self) -> ListingBatch: ...


def portal_source(portal: str) -> ListingSource:
    adapter = PORTAL_ADAPTERS.get(portal)
    if adapter is None:
        raise NotImplementedError(
            f"listings from {portal!r} are not fetched: portal scraping is deferred to the pilot; "
            "paste the listings instead (POST /v1/admin/market/listings)"
        )
    return adapter()  # pragma: no cover - no adapter yet


def _split(line: str) -> list[str]:
    for delimiter in ("\t", ";", "|"):
        if delimiter in line:
            return [f.strip() for f in line.split(delimiter)]
    return [f.strip() for f in line.rsplit(",", 2)]


class PastedListings:
    """Listings pasted by staff. Lines that cannot be read are reported, never guessed."""

    name = "manual_paste"

    def __init__(self, text: str, *, thousands: list[str] | None = None) -> None:
        self.text = text
        self.thousands = thousands or []

    def fetch(self) -> ListingBatch:
        lines = [line.rstrip() for line in self.text.replace("\r\n", "\n").split("\n")]
        batch = ListingBatch(listings=[], lines=[line for line in lines if line.strip()])
        seen_data = False
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                batch.skipped.append(Skipped("comment", row=number, detail=line[:200]))
                continue
            fields = [f for f in _split(line)]
            if len(fields) < 3 or not fields[0]:
                batch.skipped.append(Skipped("listing_unreadable", row=number, detail=line[:200]))
                continue
            location = ", ".join(f for f in fields[:-2] if f)
            price_cell, date_cell = fields[-2], fields[-1]
            amount = parse_amount(price_cell)
            if amount is None or amount.is_range:
                reason = (
                    "header"
                    if not seen_data and parse_date(date_cell) is None
                    else ("listing_price_unreadable")
                )
                batch.skipped.append(Skipped(reason, row=number, detail=line[:200]))
                continue
            unit = parse_unit(price_cell, thousands=self.thousands)
            if unit.currency == "other":
                batch.skipped.append(Skipped("currency_not_eur", row=number, detail=line[:200]))
                continue
            listed_on = parse_date(cell_text(date_cell))
            if listed_on is None:
                batch.skipped.append(Skipped("listing_date_missing", row=number, detail=line[:200]))
                continue
            seen_data = True
            batch.listings.append(
                Listing(
                    line=number,
                    location=location,
                    price_eur_m2=round(amount.values[0] * unit.to_eur_m2, 4),
                    listed_on=listed_on,
                    raw=line,
                )
            )
        return batch


def percentile(values: list[float], p: float) -> float:
    """Linear interpolation between closest ranks (the common default), ``values`` sorted."""
    if not values:
        raise ValueError("no values")
    position = (len(values) - 1) * p / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
