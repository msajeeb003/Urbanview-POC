"""Market-data imports from the command line (run from ``backend/``, which reads ``.env``).

    python -m core.market import FILE --kind statistics|client_ranges [--source S]
                                      [--retrieved YYYY-MM-DD] [--notes TEXT] [--llm MODE]
                                      [--dry-run]

``import`` records the table in ``market_imports`` and normalises it inline into ``market_data``
rows waiting for review, like the ``import_market_data`` job (the API path also keeps the
original file in the bucket). ``--dry-run`` reads, maps and prints the normalised inputs and
the report without writing anything. ``--llm never`` keeps the LLM out (rules only); the
default follows ``MARKET_NORMALISE_LLM``. Approving happens in the review queue
(``/v1/admin/review/market-inputs``), never here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from core.market.normalise import normalise_table
from core.market.pipeline import (
    MarketImporter,
    MarketImportError,
    load_context,
    model_from_settings,
)
from core.market.readers import ReadError, read_table


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.market", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--municipality", default=None, help="profile id (default: settings)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import")
    p.add_argument("file", type=Path)
    p.add_argument("--source", default=None)
    p.add_argument("--retrieved", type=date.fromisoformat, default=date.today())
    p.add_argument("--notes", default=None)
    p.add_argument("--llm", choices=("auto", "never", "always"), default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--kind", choices=("statistics", "client_ranges"), required=True)
    return parser


def _print_result(result: Any) -> None:
    for item in result.inputs:
        print(
            f"zone {item.zone_id:>4}  {item.metric:<11}  low {item.low!s:>10}  "
            f"expected {item.expected:>10}  high {item.high!s:>10}  {item.range_basis:<11}  "
            f"{item.source_date}  {','.join(item.flags)}"
        )
    report = result.report()
    print(
        json.dumps(
            {k: report[k] for k in ("normaliser", "skipped_by_reason", "issues")},
            ensure_ascii=False,
            indent=2,
        )
    )


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from core.config import get_settings
    from core.db import create_engine
    from core.municipality import load_market_profile, load_profile

    settings = get_settings()
    municipality = load_profile(args.municipality or settings.municipality_id)
    engine = create_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        mode = args.llm or settings.market_normalise_llm
        settings = settings.model_copy(update={"market_normalise_llm": mode})
        importer = MarketImporter(
            sessions,
            municipality_id=municipality.id,
            municipality_name=municipality.name,
            settings=settings,
            model_factory=lambda: model_from_settings(settings),
        )
        data = args.file.read_bytes()
        source = args.source or (
            load_market_profile(municipality.id).statistics_source
            if args.kind == "statistics"
            else None
        )
        if not source:
            print("--source is required (who published or sent the figures)", file=sys.stderr)
            return 2
        if args.dry_run:
            async with sessions() as session:
                ctx = await load_context(
                    session,
                    municipality_id=municipality.id,
                    municipality_name=municipality.name,
                    kind=args.kind,
                    source=source,
                    retrieved_on=args.retrieved,
                    mode=mode,
                    low_confidence=settings.market_low_confidence,
                    llm_max_rows=settings.market_llm_max_rows,
                )
            model = model_from_settings(settings) if mode != "never" else None
            try:
                table = read_table(args.file.name, data, max_rows=settings.market_max_rows)
            except ReadError as exc:
                print(f"cannot read {args.file}: {exc}", file=sys.stderr)
                return 2
            result = normalise_table(table, ctx, model)
            _print_result(result)
            return 0
        async with sessions() as session:
            try:
                created = await importer.record_table(
                    session,
                    kind=args.kind,
                    source=source,
                    retrieved_on=args.retrieved,
                    filename=args.file.name,
                    data=data,
                    notes=args.notes,
                )
            except MarketImportError as exc:
                print(f"cannot import {args.file}: {exc}", file=sys.stderr)
                return 2
            await session.commit()
        print(f"import {created.id} ({'new' if created.created else 'already recorded'})")
        summary = await importer.normalise(created.id)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
