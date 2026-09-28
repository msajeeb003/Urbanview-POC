"""The database side of market-data imports: record an import as it arrived, normalise it into
``market_data`` rows waiting for review (one transaction), and the context normalisation needs
(the zones, their configured range factors).

Used by the API (creating imports), the ``import_market_data`` job (normalising) and the CLI
(``python -m core.market``). Nothing here touches ``financial_assumptions``: only an approved
review decision writes a new assumptions version (``api.services.market``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.extraction.llm import StructuredModel
from core.market.listings import PastedListings
from core.market.model import (
    ImportKind,
    Metric,
    NormaliseResult,
    RangeFactors,
    RawTable,
    ZoneRef,
)
from core.market.normalise import NormaliseContext, normalise_listings, normalise_table
from core.market.readers import read_table, sha256_hex
from core.municipality import load_market_profile

log = logging.getLogger("urbanview.market")

ZONES_SQL = text("SELECT id, name FROM zones WHERE municipality_id = :m ORDER BY id")
FACTORS_SQL = text(
    """
    SELECT id, zone_id, range_low_factor, range_high_factor
    FROM financial_assumptions
    WHERE municipality_id = :m AND is_current
    """
)
IMPORT_BY_CHECKSUM_SQL = text(
    "SELECT id FROM market_imports WHERE municipality_id = :m AND kind = :kind AND sha256 = :sha"
)
INSERT_IMPORT_SQL = text(
    """
    INSERT INTO market_imports (municipality_id, kind, source, retrieved_on, file_id, filename,
                                sha256, raw, row_count, notes, created_by, created_by_user_id)
    VALUES (:m, :kind, :source, :retrieved_on, :file_id, :filename, :sha, CAST(:raw AS jsonb),
            :row_count, :notes, :created_by, :created_by_user_id)
    RETURNING id
    """
)
IMPORT_SQL = text(
    """
    SELECT id, kind, source, retrieved_on, file_id, filename, sha256, raw, status, job_id
    FROM market_imports WHERE municipality_id = :m AND id = :id
    """
)
ITEM_COUNT_SQL = text("SELECT count(*) FROM market_data WHERE import_id = :id")
INSERT_ITEM_SQL = text(
    """
    INSERT INTO market_data (municipality_id, import_id, zone_id, metric, currency, unit, low,
                             expected, high, range_basis, source, source_date, confidence, notes,
                             flags, raw, mapping, normaliser)
    VALUES (:m, :import_id, :zone_id, :metric, :currency, :unit, :low, :expected, :high,
            :range_basis, :source, :source_date, :confidence, :notes, CAST(:flags AS jsonb),
            CAST(:raw AS jsonb), CAST(:mapping AS jsonb), :normaliser)
    """
)
MARK_NORMALISED_SQL = text(
    """
    UPDATE market_imports
    SET status = 'normalised', normaliser = :normaliser, report = CAST(:report AS jsonb),
        normalised_at = :at, error = NULL, job_id = COALESCE(:job_id, job_id)
    WHERE id = :id
    """
)
MARK_FAILED_SQL = text(
    """
    UPDATE market_imports SET status = 'failed', error = :error, job_id = COALESCE(:job_id, job_id)
    WHERE id = :id AND status <> 'normalised'
    """
)
SET_JOB_SQL = text("UPDATE market_imports SET job_id = :job_id WHERE id = :id")


class MarketImportError(ValueError):
    """The import cannot be recorded (unreadable file, empty paste): a 422 for the API."""


@dataclass(frozen=True, slots=True)
class CreatedImport:
    id: int
    created: bool
    sha256: str
    row_count: int


def listings_checksum(metric: str, lines: list[str]) -> str:
    return hashlib.sha256(("\n".join([f"metric={metric}", *lines])).encode("utf-8")).hexdigest()


async def load_context(
    session: AsyncSession,
    *,
    municipality_id: str,
    municipality_name: str,
    kind: ImportKind,
    source: str,
    retrieved_on: date,
    mode: str = "auto",
    low_confidence: float = 0.7,
    llm_max_rows: int = 150,
) -> NormaliseContext:
    profile = load_market_profile(municipality_id)
    aliases = {name.casefold(): tuple(values) for name, values in profile.zone_aliases.items()}
    zones = [
        ZoneRef(int(r["id"]), r["name"], aliases.get(r["name"].casefold(), ()))
        for r in (await session.execute(ZONES_SQL, {"m": municipality_id})).mappings()
    ]
    factors = {
        (int(r["zone_id"]) if r["zone_id"] is not None else None): RangeFactors(
            float(r["range_low_factor"]),
            float(r["range_high_factor"]),
            int(r["id"]),
            r["zone_id"],
        )
        for r in (await session.execute(FACTORS_SQL, {"m": municipality_id})).mappings()
    }
    return NormaliseContext(
        municipality=municipality_name,
        kind=kind,
        source=source,
        retrieved_on=retrieved_on,
        zones=zones,
        profile=profile,
        factors=factors,
        mode=mode,  # type: ignore[arg-type]
        low_confidence=low_confidence,
        llm_max_rows=llm_max_rows,
    )


class MarketImporter:
    """Record and normalise market-data imports for one municipality."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        municipality_id: str,
        municipality_name: str,
        settings: Any,
        model_factory: Callable[[], StructuredModel | None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.municipality_id = municipality_id
        self.municipality_name = municipality_name
        self.settings = settings
        self.model_factory = model_factory
        self.clock = clock

    # --- recording --------------------------------------------------------------------------------

    async def _record(
        self,
        session: AsyncSession,
        *,
        kind: str,
        source: str,
        retrieved_on: date,
        sha: str,
        raw: dict[str, Any],
        row_count: int,
        file_id: int | None,
        filename: str | None,
        notes: str | None,
        created_by: str,
        created_by_user_id: int | None,
    ) -> CreatedImport:
        params = {"m": self.municipality_id, "kind": kind, "sha": sha}
        existing = (await session.execute(IMPORT_BY_CHECKSUM_SQL, params)).scalar_one_or_none()
        if existing is not None:
            return CreatedImport(int(existing), False, sha, row_count)
        new_id = (
            await session.execute(
                INSERT_IMPORT_SQL,
                {
                    **params,
                    "source": source,
                    "retrieved_on": retrieved_on,
                    "file_id": file_id,
                    "filename": filename,
                    "raw": json.dumps(raw, ensure_ascii=False, default=str),
                    "row_count": row_count,
                    "notes": notes,
                    "created_by": created_by,
                    "created_by_user_id": created_by_user_id,
                },
            )
        ).scalar_one()
        return CreatedImport(int(new_id), True, sha, row_count)

    async def record_table(
        self,
        session: AsyncSession,
        *,
        kind: str,
        source: str,
        retrieved_on: date,
        filename: str,
        data: bytes,
        file_id: int | None = None,
        notes: str | None = None,
        created_by: str = "cli",
        created_by_user_id: int | None = None,
    ) -> CreatedImport:
        """Read an uploaded table and record it (the same checksum and kind: the existing
        import). Raises :class:`MarketImportError` for a file that is not a readable table."""
        from core.market.readers import ReadError

        try:
            table = read_table(filename, data, max_rows=self.settings.market_max_rows)
        except ReadError as exc:
            raise MarketImportError(str(exc)) from exc
        return await self._record(
            session,
            kind=kind,
            source=source,
            retrieved_on=retrieved_on,
            sha=sha256_hex(data),
            raw=table.to_json(),
            row_count=table.row_count,
            file_id=file_id,
            filename=filename,
            notes=notes,
            created_by=created_by,
            created_by_user_id=created_by_user_id,
        )

    async def record_listings(
        self,
        session: AsyncSession,
        *,
        source: str,
        retrieved_on: date,
        metric: Metric,
        pasted: str,
        notes: str | None = None,
        created_by: str = "cli",
        created_by_user_id: int | None = None,
    ) -> CreatedImport:
        batch = PastedListings(pasted).fetch()
        if not batch.lines:
            raise MarketImportError("nothing was pasted")
        return await self._record(
            session,
            kind="listings",
            source=source,
            retrieved_on=retrieved_on,
            sha=listings_checksum(metric, batch.lines),
            raw={"format": "listings", "metric": metric, "lines": batch.lines},
            row_count=len(batch.lines),
            file_id=None,
            filename=None,
            notes=notes,
            created_by=created_by,
            created_by_user_id=created_by_user_id,
        )

    # --- normalising ------------------------------------------------------------------------------

    async def normalise(self, import_id: int, *, job_id: int | None = None) -> dict[str, Any]:
        """Normalise one import into pending ``market_data`` rows (idempotent: an import already
        normalised is reported as it is)."""
        async with self.session_factory() as session:
            row = (
                (await session.execute(IMPORT_SQL, {"m": self.municipality_id, "id": import_id}))
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError(f"no market import {import_id}")
            if row["status"] == "normalised":
                count = (await session.execute(ITEM_COUNT_SQL, {"id": import_id})).scalar_one()
                return {"import_id": import_id, "already_normalised": True, "items": int(count)}
            ctx = await load_context(
                session,
                municipality_id=self.municipality_id,
                municipality_name=self.municipality_name,
                kind=row["kind"],
                source=row["source"],
                retrieved_on=row["retrieved_on"],
                mode=self.settings.market_normalise_llm,
                low_confidence=self.settings.market_low_confidence,
                llm_max_rows=self.settings.market_llm_max_rows,
            )
        model = None
        if ctx.mode != "never" and self.model_factory is not None:
            model = self.model_factory()
        raw = row["raw"]
        if row["kind"] == "listings":
            batch = PastedListings("\n".join(raw["lines"])).fetch()
            result = normalise_listings(
                batch,
                ctx,
                metric=raw["metric"],
                min_listings=self.settings.market_min_listings,
                low_percentile=self.settings.market_listings_low_percentile,
                high_percentile=self.settings.market_listings_high_percentile,
                model=model,
            )
        else:
            result = normalise_table(RawTable.from_json(raw), ctx, model)
        if model is None and ctx.mode != "never":
            result.issues.append("no LLM configured: the rules alone mapped this import")
        await self.store(import_id, result, job_id=job_id)
        return summary(import_id, result)

    async def store(
        self, import_id: int, result: NormaliseResult, *, job_id: int | None = None
    ) -> None:
        async with self.session_factory() as session:
            for item in result.inputs:
                await session.execute(
                    INSERT_ITEM_SQL,
                    {
                        "m": self.municipality_id,
                        "import_id": import_id,
                        "zone_id": item.zone_id,
                        "metric": item.metric,
                        "currency": item.currency,
                        "unit": item.unit,
                        "low": item.low,
                        "expected": item.expected,
                        "high": item.high,
                        "range_basis": item.range_basis,
                        "source": item.source,
                        "source_date": item.source_date,
                        "confidence": item.confidence,
                        "notes": item.notes,
                        "flags": json.dumps(item.flags),
                        "raw": json.dumps(item.raw, ensure_ascii=False, default=str),
                        "mapping": json.dumps(item.mapping, ensure_ascii=False, default=str),
                        "normaliser": item.normaliser,
                    },
                )
            await session.execute(
                MARK_NORMALISED_SQL,
                {
                    "id": import_id,
                    "normaliser": result.normaliser,
                    "report": json.dumps(result.report(), ensure_ascii=False, default=str),
                    "at": self.clock(),
                    "job_id": job_id,
                },
            )
            await session.commit()

    async def mark_failed(self, import_id: int, error: str, *, job_id: int | None = None) -> None:
        async with self.session_factory() as session:
            await session.execute(
                MARK_FAILED_SQL, {"id": import_id, "error": error[:2000], "job_id": job_id}
            )
            await session.commit()


def summary(import_id: int, result: NormaliseResult) -> dict[str, Any]:
    report = result.report()
    return {
        "import_id": import_id,
        "items": report["inputs"],
        "zones": report["zones"],
        "metrics": report["metrics"],
        "skipped_by_reason": report["skipped_by_reason"],
        "issues": result.issues,
        "normaliser": result.normaliser,
        "llm": result.llm,
    }


def model_from_settings(settings: Any) -> StructuredModel | None:
    """The Claude adapter for the LLM step, or None when the ``ai`` extra is not installed."""
    try:
        from core.extraction.llm import ClaudeModel

        key = settings.anthropic_api_key
        return ClaudeModel(
            settings.market_model or settings.extraction_model,
            api_key=key.get_secret_value() if key else None,
            effort=settings.market_effort,
            adaptive_thinking=settings.extraction_adaptive_thinking,
            max_tokens=settings.market_max_tokens,
            timeout_seconds=settings.extraction_timeout_seconds,
            base_url=settings.anthropic_base_url,
        )
    except Exception as exc:  # noqa: BLE001 - no SDK or no key: the rules work alone
        log.warning("market normalisation runs without an LLM: %s", exc)
        return None
