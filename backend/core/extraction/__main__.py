"""Command line of the extraction contract (run from backend/).

python -m core.extraction export
    Write the canonical JSON Schema (core/extraction/schemas/) and the structured-output
    schemas of the current prompt set (prompt_sets/v<version>/responses/).
python -m core.extraction eval
    Ask the configured model (EXTRACTION_MODEL, ANTHROPIC_API_KEY; costs tokens) the
    never-guessed cases. Exit 1 when a never-guessed case fails.
python -m core.extraction corpus ...
    The evaluation corpus of the client's documents (``core.extraction.evalcli``).
python -m core.extraction prepared build | load ...
    Planning values prepared from a parameter table without the model, staged as approved items
    (``core.extraction.prepared``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def _eval() -> int:
    from core.config import get_settings
    from core.extraction.evaluate import model_from_settings, run_absent_cases

    threshold = get_settings().extraction_low_confidence
    model = model_from_settings()
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
    commands.add_parser("eval", help="ask the model the never-guessed cases (costs tokens)")
    from core.extraction import evalcli, prepared

    evalcli.add_parser(commands)
    prepared.add_parser(commands)
    args = parser.parse_args(argv)
    if args.command == "corpus":
        return evalcli.run(args)
    if args.command == "prepared":
        return prepared.run(args)
    if args.command == "export":
        for path in export():
            print(path)
        return 0
    return _eval()


if __name__ == "__main__":
    sys.exit(main())
