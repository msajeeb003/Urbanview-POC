"""LLM cost estimation for extraction jobs: prices are configuration, never code.

``LLM_PRICE_EUR_PER_MTOK_INPUT`` / ``_OUTPUT`` are the defaults; ``LLM_PRICE_TABLE``
(``"model=in:out,..."``, EUR per million tokens) overrides per model. Extraction tasks report the
model and the token counts; the base task stores the estimate on the job so spend per document
can be summed (``GET /v1/admin/jobs/costs``).
"""

from __future__ import annotations

from jobs.base import JobCost


def parse_price_table(raw: str | None) -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        model, sep, prices = entry.partition("=")
        price_in, sep2, price_out = prices.partition(":")
        if not sep or not sep2 or not model.strip():
            raise ValueError("LLM_PRICE_TABLE entries look like 'model=in:out' (EUR per MTok)")
        try:
            table[model.strip()] = (float(price_in), float(price_out))
        except ValueError:
            raise ValueError(f"LLM_PRICE_TABLE: cannot read {entry!r}") from None
    return table


def estimate_cost_eur(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    default_in: float,
    default_out: float,
    table: dict[str, tuple[float, float]] | None = None,
) -> float:
    price_in, price_out = (table or {}).get(model or "", (default_in, default_out))
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 4)


def cost_for(model: str | None, tokens_in: int, tokens_out: int) -> JobCost:
    """A :class:`JobCost` priced from the current settings."""
    from core.config import get_settings

    settings = get_settings()
    return JobCost(
        llm_model=model,
        tokens_in=int(tokens_in),
        tokens_out=int(tokens_out),
        estimated_cost_eur=estimate_cost_eur(
            model,
            int(tokens_in),
            int(tokens_out),
            default_in=settings.llm_price_eur_per_mtok_input,
            default_out=settings.llm_price_eur_per_mtok_output,
            table=parse_price_table(settings.llm_price_table),
        ),
    )
