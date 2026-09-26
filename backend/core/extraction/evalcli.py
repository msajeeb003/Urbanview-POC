"""``python -m core.extraction corpus ...``: the corpus gold sets, the evaluation and its log.

    corpus label [--doc ID] [--force]         draft gold sets from the table grids
    corpus eval  [--doc ID ...] [--pages 1-3,7] [--live | --replay] [--concurrency N]
                 [--prompt-version V] [--note TEXT] [--no-log] [--check]
    corpus check [--run FILE]                 the last (or a given) run against baseline.json
    corpus baseline [--run FILE]              accept a run as the new baseline
    corpus report                             rewrite RESULTS.md from the results log

A run prints a per-document table (accuracy, the six outcomes, hallucinated = false values,
wrong page citations, parcels found / missed / extra, tokens, cost), a per-field table and the
confidence calibration, writes the full report (with examples of every error) to the cache folder,
and appends its numbers to ``tests/corpus/results/log.jsonl`` (one line per run; the iteration
log). ``--check`` exits 1 when a document's accuracy falls below the baseline or any value is
hallucinated or cited on the wrong page (the CI regression gate).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.extraction.corpus import (
    CORPUS_DIR,
    Corpus,
    build_gold,
    gold_path,
    load_corpus,
    load_gold,
    save_gold,
)
from core.extraction.preprocess import PREPROCESS_VERSION
from core.extraction.prompts import PROMPT_VERSION
from core.extraction.schema import SCHEMA_VERSION
from core.extraction.scoring import OUTCOMES, DocumentScore, score_document

BACKEND = Path(__file__).resolve().parents[2]
CACHE_DIR = Path(os.environ.get("EXTRACTION_EVAL_CACHE") or BACKEND / ".cache" / "extraction-eval")
RESULTS_DIR = CORPUS_DIR / "results"
LOG = RESULTS_DIR / "log.jsonl"
BASELINE = CORPUS_DIR / "baseline.json"
RESULTS_MD = CORPUS_DIR / "RESULTS.md"
ACCURACY_SLACK = 0.005  # a run may be half a point below the baseline (model variance)


def _pages(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part.strip():
            out.add(int(part))
    return out


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


# --- label ----------------------------------------------------------------------------------------


def cmd_label(corpus: Corpus, doc_ids: list[str], force: bool, by: str) -> int:
    from core.extraction.harness import document_pages

    for doc_id in doc_ids or corpus.ids:
        doc = corpus.get(doc_id)
        path = gold_path(doc.id)
        if path.is_file() and not force:
            existing = load_gold(doc.id)
            if existing.verification:
                print(f"{doc.id}: verified gold set kept ({path}); --force to redraft")
                continue
        pages = document_pages(doc, CACHE_DIR, municipality=corpus.municipality)
        gold = build_gold(
            doc, pages, corpus.fields, municipality=corpus.municipality, labelled_by=by
        )
        save_gold(gold)
        stated = sum(1 for p in gold.parcels for v in p.values.values() if v is not None)
        print(
            f"{doc.id}: {len(gold.parcels)} parcels, {stated} stated values, "
            f"{len(gold.blocks)} block rows -> {path}"
        )
    return 0


# --- eval -----------------------------------------------------------------------------------------


def _model(args: argparse.Namespace, prompt_version: str):
    from core.config import get_settings
    from core.extraction.harness import CachingModel
    from core.extraction.llm import ClaudeModel

    settings = get_settings()
    name = args.model or settings.extraction_model
    inner = None
    if args.live:
        key = settings.anthropic_api_key
        inner = ClaudeModel(
            name,
            api_key=key.get_secret_value() if key else None,
            effort=args.effort or settings.extraction_effort,
            adaptive_thinking=settings.extraction_adaptive_thinking,
            max_tokens=settings.extraction_max_tokens,
            refusal_fallback=settings.extraction_refusal_fallback,
            timeout_seconds=settings.extraction_timeout_seconds,
            base_url=settings.anthropic_base_url,
        )
    effort = args.effort or settings.extraction_effort
    cache = CACHE_DIR / "replies" / f"{name}-effort-{effort}"
    return CachingModel(inner, cache, name=name, replay=not args.live), name, effort


def print_tables(rows: list[dict[str, Any]], scores: list[DocumentScore]) -> None:
    print()
    print(
        "| document | cells | accuracy | stated accuracy | exact | tolerance | wrong | "
        "false blank | false value | correct blank | wrong page | wrong cell | parcels found | "
        "tokens in/out | cost |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        o = row["outcomes"]
        p = row["parcels"]
        print(
            f"| {row['document']} | {row['cells']} | {_pct(row['accuracy'])} | "
            f"{_pct(row['stated_accuracy'])} ({row['stated_cells']}) | {o['exact']} | "
            f"{o['tolerance']} | {o['wrong']} | {o['missing']} | **{o['false_value']}** | "
            f"{o['correct_blank']} | {row['wrong_page']} | {row['wrong_cell']} | "
            f"{p['found']}/{p['gold']}"
            f"{' +' + str(len(p['extra'])) + ' extra' if p['extra'] else ''} | "
            f"{row['tokens_in']}/{row['tokens_out']} | "
            f"{'$' + format(row['cost_usd'], '.2f') if row['cost_usd'] is not None else '-'} |"
        )
    for score in scores:
        print(f"\n{score.document}: per field")
        print(
            "| field | accuracy | stated accuracy | "
            + " | ".join(OUTCOMES)
            + " | wrong page | wrong cell |"
        )
        print("|---" * (len(OUTCOMES) + 5) + "|")
        for key, stats in score.fields.items():
            print(
                f"| {key} | {_pct(stats.accuracy)} | {_pct(stats.stated_accuracy)} | "
                + " | ".join(str(stats.outcomes[o]) for o in OUTCOMES)
                + f" | {stats.wrong_page} | {stats.wrong_cell} |"
            )
        calibration = ", ".join(
            f"{bucket}: {_pct(c / t if t else None)} of {t}"
            for bucket, (c, t) in sorted(score.calibration.items())
        )
        print(f"confidence calibration: {calibration or '-'}")
        print(f"document fields: {score.document_fields}")


def cmd_eval(args: argparse.Namespace, corpus: Corpus) -> int:
    from core.extraction.harness import document_pages, run_document

    prompt_version = args.prompt_version or PROMPT_VERSION
    model, name, effort = _model(args, prompt_version)
    only = _pages(args.pages)
    rows: list[dict[str, Any]] = []
    scores: list[DocumentScore] = []
    reports: dict[str, Any] = {}
    for doc_id in args.doc or corpus.ids:
        doc = corpus.get(doc_id)
        gold = load_gold(doc.id)
        pages = document_pages(doc, CACHE_DIR, municipality=corpus.municipality)
        print(f"{doc.id}: {doc.pages} pages, reading ...", flush=True)
        run = run_document(
            doc,
            pages,
            model=model,
            municipality=corpus.municipality,
            prompt_version=prompt_version,
            only_pages=only,
            concurrency=args.concurrency,
            progress=not args.quiet,
        )
        score = score_document(gold, run.results, municipality=corpus.municipality, pages=only)
        scores.append(score)
        row = score.to_json()
        examples = row.pop("examples")
        row.update(
            tokens_in=run.usage.tokens_in,
            tokens_out=run.usage.output_tokens,
            cost_usd=run.cost_usd,
            seconds=round(run.seconds, 1),
            steps=len(run.steps),
            steps_failed=[
                {
                    "chunk": s.chunk_id,
                    "task": s.task,
                    "pages": s.pages,
                    "error": (s.error or "")[:300],
                }
                for s in run.failed
            ],
            live_requests=run.live_requests,
            cached_requests=run.cached_requests,
            model_version=run.model_version,
            issues=sum(len(r.issues) for r in run.results),
        )
        rows.append(row)
        reports[doc.id] = {**row, "examples": examples}
    print_tables(rows, scores)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "run": stamp,
        "prompt_version": prompt_version,
        "schema_version": SCHEMA_VERSION,
        "preprocess_version": PREPROCESS_VERSION,
        "git": _git_rev(),
        "model": name,
        "effort": effort,
        "pages": sorted(only) if only else "all",
        "note": args.note,
        "incomplete": sum(len(r["steps_failed"]) for r in rows),
        "documents": rows,
    }
    report_path = CACHE_DIR / "runs" / f"{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({**record, "reports": reports}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"\nfull report (with examples): {report_path}")
    if not args.no_log:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        write_report()
        print(f"logged in {LOG.relative_to(BACKEND)} and {RESULTS_MD.relative_to(BACKEND)}")
    if record["incomplete"]:
        print(
            f"\nINCOMPLETE: {record['incomplete']} model request(s) failed (see steps_failed): "
            "the scores above are not a measurement of the prompts"
        )
        return 1
    if args.check:
        return check(record)
    return 1 if any(r["outcomes"]["false_value"] for r in rows) else 0


def _git_rev() -> str | None:
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "core/extraction"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return rev + ("+dirty" if dirty else "")


# --- regression check -----------------------------------------------------------------------------


def check(record: dict[str, Any], baseline_path: Path = BASELINE) -> int:
    if not baseline_path.is_file():
        print("no baseline.json: nothing to compare with")
        return 1 if any(r["hallucinated"] for r in record["documents"]) else 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = []
    for row in record["documents"]:
        base = baseline["documents"].get(row["document"])
        if row["hallucinated"]:
            problems.append(f"{row['document']}: {row['hallucinated']} hallucinated value(s)")
        if row["wrong_page"] > (base or {}).get("wrong_page", 0):
            problems.append(f"{row['document']}: {row['wrong_page']} wrong page citation(s)")
        if base and row["accuracy"] + ACCURACY_SLACK < base["accuracy"]:
            problems.append(
                f"{row['document']}: accuracy {_pct(row['accuracy'])} below the baseline "
                f"{_pct(base['accuracy'])}"
            )
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("check passed: no hallucination, citations and accuracy at the baseline")
    return 1 if problems else 0


def _last_record(run: Path | None) -> dict[str, Any]:
    if run is not None:
        return json.loads(run.read_text(encoding="utf-8"))
    lines = [line for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(lines[-1])


def cmd_baseline(run: Path | None) -> int:
    record = _last_record(run)
    if record["pages"] != "all":
        print("a baseline needs a full-corpus run (no --pages)")
        return 1
    baseline = {
        "accepted": datetime.now(UTC).date().isoformat(),
        "run": record["run"],
        "prompt_version": record["prompt_version"],
        "schema_version": record["schema_version"],
        "model": record["model"],
        "effort": record["effort"],
        "documents": {
            row["document"]: {
                "accuracy": row["accuracy"],
                "hallucinated": row["hallucinated"],
                "wrong_page": row["wrong_page"],
                "cells": row["cells"],
            }
            for row in record["documents"]
        },
    }
    BASELINE.write_text(json.dumps(baseline, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"baseline set from run {record['run']} -> {BASELINE}")
    return 0


def write_report() -> None:
    """RESULTS.md: one row per logged run and document (the iteration log)."""
    if not LOG.is_file():
        return
    lines = [
        "# Extraction evaluation results",
        "",
        "Generated by `python -m core.extraction corpus report` from `results/log.jsonl`"
        " (one line per run). Metrics: README.md.",
        "",
        "| run | prompt | model / effort | pages | document | accuracy | stated accuracy | exact |"
        " tolerance |"
        " wrong | false blank | false value | wrong page | parcels | cost | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        pages = record["pages"]
        pages_text = "all" if pages == "all" else f"{len(pages)} p."
        if record.get("incomplete"):
            pages_text += f" ({record['incomplete']} failed calls)"
        for row in record["documents"]:
            o = row["outcomes"]
            cost = row.get("cost_usd")
            lines.append(
                f"| {record['run']} | {record['prompt_version']} | {record['model']} / "
                f"{record['effort']} | {pages_text} | {row['document']} | "
                f"{_pct(row['accuracy'])} | {_pct(row.get('stated_accuracy'))} | "
                f"{o['exact']} | {o['tolerance']} | "
                f"{o['wrong']} | {o['missing']} | "
                f"{o['false_value']} | {row['wrong_page']} | "
                f"{row['parcels']['found']}/{row['parcels']['gold']} | "
                f"{'$' + format(cost, '.2f') if cost is not None else '-'} | "
                f"{(record.get('note') or '').replace('|', '/')} |"
            )
    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# --- entry ----------------------------------------------------------------------------------------


def add_parser(commands: Any) -> None:
    corpus = commands.add_parser("corpus", help="the evaluation corpus: gold sets, eval, check")
    actions = corpus.add_subparsers(dest="action", required=True)
    label = actions.add_parser("label", help="draft gold sets from the table grids")
    label.add_argument("--doc", action="append", default=[])
    label.add_argument("--force", action="store_true")
    label.add_argument("--by", default="core.extraction.corpus.build_gold (draft)")
    ev = actions.add_parser("eval", help="run the pipeline on the corpus and score it")
    ev.add_argument("--doc", action="append", default=[])
    ev.add_argument("--pages", help="a sample of pages, e.g. 1-3,7 (gold parcels on them)")
    mode = ev.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="call the model on cache misses")
    mode.add_argument("--replay", action="store_true", help="cached replies only (default)")
    ev.add_argument("--concurrency", type=int, default=4)
    ev.add_argument("--prompt-version")
    ev.add_argument("--model")
    ev.add_argument("--effort")
    ev.add_argument("--note")
    ev.add_argument("--no-log", action="store_true")
    ev.add_argument("--check", action="store_true", help="fail on a regression (CI)")
    ev.add_argument("--quiet", action="store_true")
    ch = actions.add_parser("check", help="compare the last run with baseline.json")
    ch.add_argument("--run", type=Path)
    base = actions.add_parser("baseline", help="accept the last full run as the baseline")
    base.add_argument("--run", type=Path)
    actions.add_parser("report", help="rewrite RESULTS.md from the log")


def run(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    if args.action == "label":
        return cmd_label(corpus, args.doc, args.force, args.by)
    if args.action == "eval":
        return cmd_eval(args, corpus)
    if args.action == "check":
        return check(_last_record(args.run))
    if args.action == "baseline":
        return cmd_baseline(args.run)
    write_report()
    return 0


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="python -m core.extraction.evalcli")
    add_parser(parser.add_subparsers(dest="command", required=True))
    sys.exit(run(parser.parse_args()))
