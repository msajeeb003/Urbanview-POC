"""A scripted stand-in for the extraction model (tests of the ``extract_document`` job).

:class:`Transcriber` answers every request with an instance of the request's response schema:
"not found" for every field, except that for the ``parameter_table`` task it copies the rows of
the tables the pages show, cell by cell, exactly as printed (the model's job in the contract).
Columns are recognised by their header words (``COLUMN_FIELDS``). Hooks let a test break one
page (``invalid_pages``), change a printed value (``override``) or fail a number of calls with
an exception (``fail_first``), and ``calls`` records what was asked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from core.extraction.llm import ModelReply, ModelUsage
from core.extraction.prompts import SystemBlock
from core.extraction.textmatch import words_fold

# header words (accent-folded) -> (field, how the unit is printed)
COLUMN_FIELDS: list[tuple[str, str, str | None]] = [
    ("broj up", "urban_parcel_number", None),
    ("blok", "block_ref", None),
    ("povrsina", "planned_parcel_area_m2", "none"),
    ("namjena", "land_use", None),
    ("spratnost", "max_floors", None),
    ("zauzetosti", "max_site_coverage_pct", "ratio"),
    ("izgradjenosti", "max_far", "none"),
]
TASK_TITLES = {
    "planning-parameter tables": "parameter_table",
    "conditions of urban parcels": "urban_parcel",
    "blocks and plan zones": "block",
    "the document": "document",
    "infrastructure": "infrastructure",
    "land-use legend": "land_use_legend",
}
_PAGE = re.compile(r"^=== Page (\d+) ===$")
_TABLE = re.compile(r"^\[Table (\S+?):")
_COLUMN = re.compile(r"c(\d+) ([^|\]]+)")
_ROW = re.compile(r"^r(\d+) \| (.*)$")


def task_of(system: Sequence[SystemBlock]) -> str:
    for block in system:
        match = re.search(r"# Task: (.+)", block.text)
        if match:
            return TASK_TITLES.get(match.group(1).strip(), match.group(1).strip())
    raise AssertionError("no task in the system prompt")


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref:
        return schema["$defs"][ref.rsplit("/", 1)[-1]]
    return node


def absent(schema: dict[str, Any], node: dict[str, Any] | None = None) -> Any:
    """A minimal valid instance of a strict response schema: nothing found anywhere."""
    node = _resolve(schema, node or schema)
    if "anyOf" in node:
        options = [_resolve(schema, o) for o in node["anyOf"]]
        if any(o.get("type") == "null" for o in options):
            return None
        return absent(schema, options[0])
    kind = node.get("type")
    if kind == "object":
        props = node.get("properties", {})
        if "absent_reason" in props and "value" in props:  # an OutValue
            return {
                "value": None,
                "unit": None,
                "raw_text": None,
                "page": None,
                "table_ref": None,
                "confidence": 1.0,
                "absent_reason": "not_found",
            }
        return {name: absent(schema, sub) for name, sub in props.items()}
    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in ("number", "integer"):
        return 0
    if "enum" in node:
        return node["enum"][0]
    return ""


def tables(user: str) -> list[dict[str, Any]]:
    """The tables of the rendered pages: id, page, column names and rows of cells."""
    out: list[dict[str, Any]] = []
    page = None
    current: dict[str, Any] | None = None
    for line in user.splitlines():
        if m := _PAGE.match(line):
            page = int(m.group(1))
        elif (m := _TABLE.match(line)) is not None:
            columns = {int(i): name.strip() for i, name in _COLUMN.findall(line.split(":", 1)[1])}
            current = {"id": m.group(1), "page": page, "columns": columns, "rows": []}
            out.append(current)
        elif line.startswith("[End of table"):
            current = None
        elif current is not None and (m := _ROW.match(line)):
            cells = {}
            for part in m.group(2).split(" | "):
                col, _, value = part.partition(": ")
                cells[int(col[1:])] = value
            current["rows"].append((int(m.group(1)), cells))
    return out


class Transcriber:
    def __init__(
        self,
        *,
        name: str = "claude-test-model",
        invalid_pages: Sequence[int] = (),
        override: dict[tuple[str, str], str] | None = None,
        fail_first: int = 0,
        fail_calls: Sequence[int] = (),
        failure: Callable[[], Exception] | None = None,
        low_confidence: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.invalid_pages = set(invalid_pages)
        self.override = override or {}
        self.fail_first = fail_first
        self.fail_calls = set(fail_calls)  # 1-based call numbers that raise ``failure``
        self.failure = failure
        self.low_confidence = set(low_confidence)
        self.calls: list[tuple[str, list[int], str]] = []

    def complete(
        self, *, system: Sequence[SystemBlock], user: str, schema: dict[str, Any]
    ) -> ModelReply:
        task = task_of(system)
        pages = [int(p) for p in re.findall(r"^=== Page (\d+) ===$", user, re.M)]
        self.calls.append((task, pages, user))
        if self.fail_first > 0 or len(self.calls) in self.fail_calls:
            self.fail_first = max(0, self.fail_first - 1)
            assert self.failure is not None
            raise self.failure()
        usage = ModelUsage(input_tokens=1000, output_tokens=200)
        if task == "parameter_table" and self.invalid_pages & set(pages):
            return ModelReply(data={"urban_parcels": "not a list"}, model=self.name, usage=usage)
        data = absent(schema)
        if task == "parameter_table":
            data["urban_parcels"] = self._parcels(schema, user)
        return ModelReply(data=data, model=self.name, usage=usage)

    def _parcels(self, schema: dict[str, Any], user: str) -> list[dict[str, Any]]:
        """Compact parcels (prompt set 1.1): only the fields the row states."""
        out = []
        for table in tables(user):
            fields = {}
            for index, name in table["columns"].items():
                folded = words_fold(name)
                for words, field, unit in COLUMN_FIELDS:
                    if words in folded:
                        fields[index] = (field, unit, name)
                        break
            number_col = next((i for i, f in fields.items() if f[0] == "urban_parcel_number"), None)
            block_col = next((i for i, f in fields.items() if f[0] == "block_ref"), None)
            for row_index, cells in table["rows"]:
                if number_col is None or not cells.get(number_col):
                    continue
                number = cells[number_col]
                values = []
                for index, (field, unit, header) in fields.items():
                    text = cells.get(index)
                    if not text or field in ("urban_parcel_number", "block_ref"):
                        continue
                    text = self.override.get((number, field), text)
                    low = (number, field) in {tuple(k.split(":", 1)) for k in self.low_confidence}
                    values.append(
                        {
                            "field": field,
                            "value": text,
                            "unit": unit or "words",
                            "text": text,
                            "page": table["page"],
                            "table": table["id"],
                            "cell": f"r{row_index}c{index}",
                            "column": header,
                            "confidence": 0.3 if low else 0.95,
                            "status": "stated",
                        }
                    )
                block = cells.get(block_col, "") if block_col is not None else ""
                out.append(
                    {
                        "number": number,
                        "page": table["page"],
                        "table": table["id"],
                        "number_cell": f"r{row_index}c{number_col}",
                        "block": block,
                        "block_cell": f"r{row_index}c{block_col}" if block else "",
                        "confidence": 0.95,
                        "values": values,
                    }
                )
        return out
