"""Command line of the extraction contract (run from backend/).

python -m core.extraction export
    Write the canonical JSON Schema (core/extraction/schemas/) and the structured-output
    schemas of the current prompt set (prompt_sets/v<version>/responses/).
python -m core.extraction eval [--dry-run]
    Ask the configured model (EXTRACTION_MODEL, ANTHROPIC_API_KEY; costs tokens) the
    never-guessed cases. --dry-run replays the faithful responses instead. Exit 1 when a
    never-guessed case fails.
python -m core.extraction corpus ...
    The evaluation corpus of the client's documents (``core.extraction.evalcli``).
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


def _dry_model() -> ScriptedModel:
    """A scripted model answering every prompt with the faithful response."""
    from core.extraction.cases import ABSENT_CASES
    from core.extraction.pages import render_pages

    answers = {render_pages(case.pages): case.faithful for case in ABSENT_CASES}

    def script(system: Sequence[SystemBlock], user: str, schema: dict[str, Any]) -> dict:
        for pages, response in answers.items():
            if pages in user:
                return response
        raise LookupError("no scripted response for this prompt")

    return ScriptedModel(script, name="dry-run")


def _eval(dry_run: bool) -> int:
    from core.config import get_settings
    from core.extraction.evaluate import model_from_settings, run_absent_cases

    threshold = get_settings().extraction_low_confidence
    model = _dry_model() if dry_run else model_from_settings()
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
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.extraction")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="write the JSON Schemas")
    evaluation = commands.add_parser("eval", help="evaluate a model against the contract")
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
    return _eval(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
