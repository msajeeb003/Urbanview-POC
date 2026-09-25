"""Order pricing tiers and turnaround: configuration, never logic.

``ORDER_PRICE_TIERS`` is ``"<max area m²>:<price EUR>,…,inf:<price>"`` (BRD band EUR 50–200; the
client still has to confirm the tiers, the mockup uses 100 up to 500 m² and 200 above). The
price is picked server-side from the parcel's area basis (planned urban parcel area when it
exists, cadastral area otherwise).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class PriceTier:
    up_to_m2: float | None  # None = open-ended top tier
    price_eur: float


def parse_price_tiers(raw: str | None) -> list[PriceTier]:
    tiers: list[PriceTier] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        bound, sep, price = entry.partition(":")
        if not sep or not price.strip():
            raise ValueError("ORDER_PRICE_TIERS entries look like '<max m²>:<price EUR>'")
        bound = bound.strip().lower()
        try:
            up_to = None if bound in ("inf", "*") else float(bound)
            tiers.append(PriceTier(up_to_m2=up_to, price_eur=float(price)))
        except ValueError:
            raise ValueError(f"ORDER_PRICE_TIERS: cannot read tier {entry!r}") from None
    if not tiers:
        raise ValueError("ORDER_PRICE_TIERS needs at least one tier")
    if tiers[-1].up_to_m2 is not None:
        raise ValueError("ORDER_PRICE_TIERS: the last tier must be open-ended (inf)")
    bounds = [t.up_to_m2 for t in tiers[:-1]]
    if any(b is None or b <= 0 for b in bounds):
        raise ValueError("ORDER_PRICE_TIERS: only the last tier may be open-ended, bounds > 0")
    if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise ValueError("ORDER_PRICE_TIERS: tier boundaries must be strictly ascending")
    if any(t.price_eur < 0 for t in tiers):
        raise ValueError("ORDER_PRICE_TIERS: prices must not be negative")
    return tiers


def price_for(area_m2: float, tiers: list[PriceTier]) -> PriceTier:
    """The first tier whose bound covers the area (bounds are inclusive)."""
    for tier in tiers:
        if tier.up_to_m2 is None or area_m2 <= tier.up_to_m2:
            return tier
    return tiers[-1]


def add_business_days(start: date, days: int) -> date:
    """``days`` Monday-to-Friday days after ``start`` (public holidays are not modelled)."""
    current, remaining = start, max(0, int(days))
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current
