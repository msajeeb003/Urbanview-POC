"""Market-data imports: zone-level low / expected / high inputs for the feasibility engine.

Spec: ``docs/specs/market-data.md``. Market figures come from official statistics (Monstat:
selling prices and construction cost of new dwellings), the client's range sheet (per zone: land,
construction, design and sale per m², low / high) and asking prices staff paste from the listing
portals (Realitica, Estitor; scraping is deferred to the pilot). The path:

1. **record** (:mod:`core.market.pipeline`): the file (``stored_files``, kind ``market_data``) or
   the pasted text is read as it is (:mod:`core.market.readers`) into ``market_imports`` with
   its source, retrieval date and checksum (the same content twice is one import);
2. **normalise** (job ``import_market_data``, :mod:`core.market.normalise`): the rules
   (:mod:`core.market.rules`, the profile's ``[market]`` words) and, where they cannot, the LLM
   (:mod:`core.market.llm_map`: which column is which metric and bound, which zone an area name
   is, period and unit labels as printed; never a figure) map the table; code reads the figures,
   converts units and derives missing ranges only from configured range factors. One
   ``market_data`` row per zone and metric, ``pending_review``, with its source, reference date,
   confidence, flags and the raw cells;
3. **review** (``/v1/admin/review/market-inputs``, ``api.services.market``): approve, amend or
   reject; an approved or amended row writes a new ``financial_assumptions`` version for its zone
   (effective date, per-rate provenance). Only then does it reach the panel.

The CLI (``python -m core.market``) records and normalises without the queue.
"""

from core.market.model import (
    IMPORT_KINDS,
    METRICS,
    MarketInput,
    NormaliseResult,
    RawTable,
    SheetMapping,
    ZoneRef,
)

__all__ = [
    "IMPORT_KINDS",
    "METRICS",
    "MarketInput",
    "NormaliseResult",
    "RawTable",
    "SheetMapping",
    "ZoneRef",
]
