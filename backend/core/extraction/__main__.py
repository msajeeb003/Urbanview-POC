"""Command line of the extraction contract (run from backend/).

python -m core.extraction export
    Write the canonical JSON Schema (core/extraction/schemas/) and the structured-output
    schemas of the current prompt set (prompt_sets/v<version>/responses/).
python -m core.extraction check-sample DIR [--docs ROOT]
    Build every hand-labelled page of DIR into the contract (labels must be on their pages),
    write <name>.result.json and read it back with the schema. ROOT holds the source PDFs
    (default ../docs/gis/source).
python -m core.extraction eval [--sample DIR] [--docs ROOT] [--dry-run]
    Ask the configured model (EXTRACTION_MODEL, ANTHROPIC_API_KEY; costs tokens) the
    never-guessed cases and, with --sample, the hand-labelled pages. --dry-run replays the
    faithful responses instead. Exit 1 when a never-guessed case fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.extraction.llm import ScriptedModel
from core.extraction.prompts import SystemBlock
from core.extraction.schema import SCHEMA_VERSION, result_json_schema

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_DOCS = Path(__file__).resolve().parents[3] / "docs" / "gis" / "source"


def schema_path() -> Path:
    return SCHEMAS_DIR / f"extraction-v{SCHEMA_VERSION}.schema.json"


def export() -> list[Path]:
    from core.extraction.prompts import write_response_schemas

    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    target = schema_path()
    target.write_text(
        json.dumps(result_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return [target, *write_response_schemas()]


def _check_sample(sample: Path, docs: Path) -> int:
    from core.extraction.sample import check_label_file

    files = sorted(sample.glob("*.labels.json"))
    if not files:
        print(f"no *.labels.json in {sample}")
        return 1
    failed = 0
    for path in files:
        check = check_label_file(path, docs)
        stats = check.result.stats
        state = "ok" if check.ok else "FAILED"
        print(
            f"{check.name}: {state}; {stats.stated} stated, missing {stats.missing}, "
            f"flags {stats.flags}"
        )
        for problem in check.problems:
            print(f"  - {problem}")
        failed += not check.ok
    return 1 if failed else 0


def _dry_model(sample: Path | None, docs: Path) -> ScriptedModel:
    """A scripted model answering every prompt with the faithful response."""
    from core.extraction.cases import ABSENT_CASES
    from core.extraction.pages import render_pages
    from core.extraction.sample import labels_to_response, load_labels, pages_for

    answers = {render_pages(case.pages): case.faithful for case in ABSENT_CASES}
    if sample is not None:
        for path in sorted(sample.glob("*.labels.json")):
            labels = load_labels(path)
            answers[render_pages(pages_for(labels, docs))] = labels_to_response(labels)

    def script(system: Sequence[SystemBlock], user: str, schema: dict[str, Any]) -> dict:
        for pages, response in answers.items():
            if pages in user:
                return response
        raise LookupError("no scripted response for this prompt")

    return ScriptedModel(script, name="dry-run")


def _eval(sample: Path | None, docs: Path, dry_run: bool) -> int:
    from core.config import get_settings
    from core.extraction.evaluate import model_from_settings, run_absent_cases, run_sample

    threshold = get_settings().extraction_low_confidence
    model = _dry_model(sample, docs) if dry_run else model_from_settings()
    print(f"model: {model.name}")
    failed = 0
    for outcome in run_absent_cases(model, low_confidence=threshold):
        verdict = "pass" if outcome.passed else "FAIL"
        print(f"[{verdict}] {outcome.case}: {outcome.trap}")
        for where, state in outcome.absent.items():
            print(f"    absent {where}: {state}")
        for where, ok in outcome.stated.items():
            print(f"    stated {where}: {'read' if ok else 'NOT READ'}")
        failed += not outcome.passed
    if sample is not None:
        for result in run_sample(model, sample, docs, low_confidence=threshold):
            s = result.score
            print(
                f"{result.name}: {len(s.matched)} matched, {len(s.missed)} missed, "
                f"{len(s.wrong)} wrong, {len(s.extra)} extra (possible guesses), "
                f"{len(s.unverified)} removed as unverified"
            )
            for label, items in (("wrong", s.wrong), ("extra", s.extra)):
                for item in items:
                    print(f"    {label}: {item}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.extraction")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="write the JSON Schemas")
    check = commands.add_parser("check-sample", help="validate the hand-labelled sample")
    check.add_argument("sample", type=Path)
    check.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    evaluation = commands.add_parser("eval", help="evaluate a model against the contract")
    evaluation.add_argument("--sample", type=Path)
    evaluation.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    evaluation.add_argument("--dry-run", action="store_true")
    from core.extraction import evalcli

    evalcli.add_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "corpus":
        return evalcli.run(args)
    if args.command == "export":
        for path in export():
            print(path)
        return 0
    if args.command == "check-sample":
        return _check_sample(args.sample, args.docs)
    return _eval(args.sample, args.docs, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
