"""The two engines cannot drift: the TypeScript package (as the browser runs it, the built bundle)
and the Python copy must give byte-identical JSON for the same inputs, beyond the hand-made
fixtures.

A seeded generator produces hundreds of input sets over the whole input space (missing and zero
areas / FAR / coverage, no market row, per-m² and total costs, multiplier and absolute bounds,
caller defaults, visitor edits including resets to the market value, context fields, and a few
invalid inputs both engines must reject), plus pairs of parcel areas for the basis rule. One node
process runs them through ``calculate`` / ``recalculate`` / ``selectCalculationBasis``; every
result's ``JSON.stringify`` text must equal ``to_json`` of the Python result.

Skipped (with the reason) when node is missing or the bundle is not built or older than the
TypeScript sources: ``npm run build -w @urbanview/feasibility-engine``.
"""

from __future__ import annotations

import copy
import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.engine import shared

PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "feasibility-engine"
BUNDLE = PACKAGE / "dist" / "index.cjs"
SEED = 20260926
CASES = 600
NODE_SCRIPT = """
const e = require(process.argv[1]);
let raw = '';
process.stdin.on('data', (d) => { raw += d; }).on('end', () => {
  const out = JSON.parse(raw).map((c) => {
    try {
      if (c.kind === 'basis') return JSON.stringify(e.selectCalculationBasis(c.areas));
      const r = c.edits === null ? e.calculate(c.inputs) : e.recalculate(c.inputs, c.edits);
      return JSON.stringify(r);
    } catch (err) {
      return 'ERROR:' + err.name;
    }
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def _bundle_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not BUNDLE.is_file():
        pytest.skip(
            "the TypeScript engine is not built (npm run build -w @urbanview/feasibility-engine)"
        )
    newest_source = max(p.stat().st_mtime for p in (PACKAGE / "src").glob("*.ts"))
    if BUNDLE.stat().st_mtime < newest_source:
        pytest.skip("the TypeScript bundle is older than its sources: rebuild it before comparing")
    return node


def _ranged(rng: random.Random, expected: float) -> dict[str, Any]:
    if rng.random() < 0.5:
        return {
            "expected": expected,
            "bounds": {
                "kind": "multiplier",
                "low": rng.choice([0.7, 0.8, 0.86, 0.9, 1.0]),
                "high": rng.choice([1.0, 1.1, 1.15, 1.2, 1.35]),
            },
        }
    low = min(expected, round(expected * rng.uniform(0.6, 1.0), rng.choice([0, 1, 2])))
    high = max(expected, round(expected * rng.uniform(1.0, 1.5), rng.choice([0, 1, 2])))
    return {"expected": expected, "bounds": {"kind": "absolute", "low": low, "high": high}}


def _cost(rng: random.Random, rate: tuple[float, float]) -> dict[str, Any]:
    if rng.random() < 0.8:
        return {"per_m2": _ranged(rng, round(rng.uniform(*rate), rng.choice([0, 1, 2])))}
    return {"total": _ranged(rng, round(rng.uniform(1e4, 6e6), rng.choice([0, 2])))}


def _calculation_case(rng: random.Random) -> dict[str, Any]:
    planning: dict[str, Any] = {
        "plot_area": rng.choice(
            [None, 0, round(rng.uniform(40, 25000), 1), round(rng.uniform(100, 4000), 2)]
        ),
        "calculation_basis": rng.choice(["urban", "cadastral"]),
        "far": rng.choice([None, 0, round(rng.uniform(0.1, 7), 2), round(rng.uniform(0.5, 4), 1)]),
        "site_coverage_pct": rng.choice([None, 0, 100, round(rng.uniform(5, 100), 1)]),
    }
    if rng.random() < 0.4:
        planning.update(
            planned_area=rng.choice([None, round(rng.uniform(40, 5000), 1)]),
            cadastral_area=rng.choice([None, round(rng.uniform(40, 5000), 1)]),
            max_height_m=rng.choice([None, 12.5, 27.5]),
            max_floors=rng.choice([None, "P+4", "P+8+Pk"]),
            land_use=rng.choice([None, "Residential", "Mixed use"]),
        )
    inputs: dict[str, Any] = {"planning": planning, "market": None}
    if rng.random() < 0.8:
        inputs["market"] = {
            "market_value_per_m2": _ranged(
                rng, round(rng.uniform(600, 4500), rng.choice([0, 1, 2]))
            ),
            "construction_cost": _cost(rng, (400, 1600)),
            "land_value": _cost(rng, (50, 2500)),
            "design_and_documentation_costs": _cost(rng, (20, 180)),
        }
    elif rng.random() < 0.6:
        inputs["market_missing_reason"] = rng.choice(
            ["no_market_data", "no_market_data_zone_unknown"]
        )
    if rng.random() < 0.3:
        inputs["assumptions"] = {
            "saleable_share": rng.choice([0.55, 0.6, 0.65, 0.7, 0.75, 0.85, 1])
        }
    edits: dict[str, Any] | None = None
    if rng.random() < 0.5:
        edits = {}
        if rng.random() < 0.6:
            edits["saleable_share"] = rng.choice([0.5, 0.62, 0.7, 0.8, 0.95, 1])
        if rng.random() < 0.6:
            edits["construction_cost_per_m2"] = rng.choice(
                [None, 0, 650, round(rng.uniform(300, 2000), 2)]
            )
        if rng.random() < 0.6:
            edits["market_value_per_m2"] = rng.choice(
                [None, 1800, round(rng.uniform(500, 5000), 1)]
            )
    if rng.random() < 0.04:  # a caller bug both engines must reject the same way
        rng.choice(
            [
                lambda: planning.__setitem__("plot_area", -1),
                lambda: planning.__setitem__("site_coverage_pct", 100.5),
                lambda: planning.__setitem__("planned_area", -0.1),
            ]
        )()
    return {"kind": "calc", "inputs": inputs, "edits": edits}


def _basis_case(rng: random.Random) -> dict[str, Any]:
    pick = lambda: rng.choice([None, 0, round(rng.uniform(1, 9000), 1), -3])  # noqa: E731
    return {"kind": "basis", "areas": {"planned_area": pick(), "cadastral_area": pick()}}


def _python(case: dict[str, Any]) -> str:
    try:
        if case["kind"] == "basis":
            areas = case["areas"]
            return shared.to_json(
                shared.select_calculation_basis(areas["planned_area"], areas["cadastral_area"])
            )
        inputs = copy.deepcopy(case["inputs"])
        if case["edits"] is None:
            return shared.to_json(shared.calculate(inputs))
        return shared.to_json(shared.recalculate(inputs, copy.deepcopy(case["edits"])))
    except shared.EngineInputError:
        return "ERROR:EngineInputError"


def test_both_engines_give_byte_identical_json_on_generated_inputs():
    node = _bundle_or_skip()
    rng = random.Random(SEED)
    cases = [_calculation_case(rng) for _ in range(CASES)] + [_basis_case(rng) for _ in range(80)]
    completed = subprocess.run(
        [node, "-e", NODE_SCRIPT, str(BUNDLE)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    typescript = json.loads(completed.stdout)
    assert len(typescript) == len(cases)
    mismatches = [
        (index, case, python, ts)
        for index, (case, ts) in enumerate(zip(cases, typescript, strict=True))
        if (python := _python(case)) != ts
    ]
    assert not mismatches, json.dumps(mismatches[:3], indent=1)[:4000]
    # the generator really covers the space: results, rejections and every reason code
    outcomes = "".join(typescript)
    for needle in (
        '"status":"ok"',
        "ERROR:EngineInputError",
        "area_unknown",
        "far_not_stated",
        "coverage_not_stated",
        "requires_gfa",
        "no_market_data",
        "no_market_data_zone_unknown",
        '"source":"user"',
        '"basis":"total"',
        '"calculation_basis":"urban"',
    ):
        assert needle in outcomes, needle
