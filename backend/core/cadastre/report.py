"""The import report of a cadastral dataset: ``report.json`` and ``report.md`` (with ``diff.csv``).

What the reviewer signs off before publishing: provenance (source, access basis, licence,
retrieval date, checksum, CRS), the validation (errors refuse the import, warnings are listed with
samples), the KOs, the diff against the previous version and the ownership decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CHANGE_LABELS = {
    "added": "added",
    "removed": "removed (retired at publish)",
    "geometry_changed": "geometry changed",
    "attributes_changed": "attributes changed",
    "unchanged": "unchanged",
    "out_of_scope": "in KOs this export does not cover (kept)",
}


def _size(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|")


def render_markdown(report: dict[str, Any]) -> str:
    p = report["provenance"]
    v = report["validation"]
    lines = [
        f"# Cadastral dataset {report['dataset_version']}: {report['status']}",
        "",
        "| | |",
        "|---|---|",
        f"| Source | {_cell(p['source_name'])} ({p['method']}) {_cell(p.get('source_url'))} |",
        f"| Retrieved | {p['retrieved_at']} |",
        f"| Access basis | {_cell(p.get('access_basis'))} |",
        f"| Licence | {_cell(p.get('licence_note'))} |",
        f"| File | {_cell(p.get('file_name'))}, {_size(p.get('file_size'))}, "
        f"sha256 `{p.get('file_sha256')}` |",
        f"| Source CRS | {_cell(p.get('source_crs'))} → EPSG:4326; transformation: "
        f"{_cell(p.get('transform') or 'PROJ default')}; areas in EPSG:{report['area_srid']} |",
        f"| Parcels | {v['stats'].get('records', 0)} records in {len(v['stats'].get('kos', []))} "
        "KOs |",
        f"| Ownership / legal burden | {_cell(report['ownership'].get('summary'))} |",
        "",
        "## Validation",
        "",
    ]
    if report.get("refused"):
        lines += [f"**Refused:** {report['refused']}", ""]
    if v["errors"]:
        lines.append("**Errors** (nothing was staged):")
        lines.append("")
        for f in v["errors"]:
            lines.append(f"- `{f['code']}` {f['message']}: {f['count']}")
        lines.append("")
    else:
        lines += ["No errors: every geometry is valid (after repair), no duplicate parcel.", ""]
    if v["warnings"]:
        lines.append("**Warnings:**")
        lines.append("")
        for f in v["warnings"]:
            lines.append(f"- `{f['code']}` {f['message']}: {f['count']}")
        lines.append("")
    stats = v["stats"]
    cov = stats.get("coverage") or {}
    if cov:
        lines += [
            f"Coverage of the municipality's extent: {cov.get('covered')} of {cov.get('cells')} "
            f"{cov.get('cell_m')} m cells ({float(cov.get('ratio') or 0):.1%}); parcels outside "
            f"the extent: {stats.get('outside_extent', 0)}; repaired geometries: "
            f"{stats.get('repaired', 0)}.",
            "",
        ]
    area = stats.get("area_m2") or {}
    if area:
        lines += [
            f"Areas (m²): min {area.get('min_m2')}, median {area.get('median_m2')}, max "
            f"{area.get('max_m2')}, total {area.get('total_m2')}.",
            "",
        ]
    kos = stats.get("kos") or []
    if kos:
        lines += ["## Cadastral municipalities", "", "| KO | code | parcels |", "|---|---|---:|"]
        lines += [
            f"| {_cell(k['ko_name'])} | {_cell(k['ko_code'])} | {k['parcels']} |" for k in kos
        ]
        lines.append("")
    diff = report.get("diff")
    if diff:
        previous = diff.get("previous_version") or "none (first import)"
        lines += [f"## Changes against {previous}", "", "| change | parcels |", "|---|---:|"]
        for key, label in CHANGE_LABELS.items():
            lines.append(f"| {label} | {diff['totals'].get(key, 0)} |")
        lines.append("")
        if diff.get("csv_rows") is not None:
            lines += [f"Every changed parcel: `diff.csv` ({diff['csv_rows']} rows).", ""]
    for finding in (*v["errors"], *v["warnings"]):
        if finding.get("samples"):
            lines += [f"### Samples: {finding['code']}", "", "```"]
            lines += [json.dumps(s, ensure_ascii=False, default=str) for s in finding["samples"]]
            lines += ["```", ""]
    if report["status"] == "staged":
        lines += [
            "## Next",
            "",
            "Publishing (POST /v1/admin/publish) applies the dataset: parcels are upserted by KO, "
            "number and sub-number (ids stay stable), and the parcels of the imported KOs that "
            "this version no longer contains are retired (kept, not served).",
            "",
        ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return [md_path, json_path]
