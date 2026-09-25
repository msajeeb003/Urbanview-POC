"""Week-1 geometry assessment (build plan P0 gate 1): ``python -m core.gis.assess``.

    cd backend
    python -m core.gis.assess ../docs/gis/source/catalog.toml --out ../docs/gis/assessment

Reads the catalog (the client's planning documents and any GIS / CAD files, see the comment at
the top of ``docs/gis/source/catalog.toml``), inspects every file (pymupdf for PDFs, GDAL's
``ogrinfo`` for DWG / DXF / SHP / GeoPackage), classifies each plan sheet A / B / C, samples
the geometry, records the georeferencing evidence and writes ``report.md``, the CSV tables,
``assessment.json`` and layer-isolation previews into ``--out``. Needs the ``gis`` extra.

Inspection takes minutes per sheet. ``--cache FILE`` keeps its results (a pickle, outside the
repository); ``--report-only --cache FILE`` rebuilds effort, decisions and the report from it
after the effort model or the rules change, without reading the sheets again.
"""

from __future__ import annotations

import argparse
import hashlib
import pickle
import sys
import time
from pathlib import Path

import pymupdf
import shapely

from core.gis.assessment import load_catalog, refresh, run
from core.gis.report import write_all
from core.municipality import load_gis_profile


def _inputs(catalog: Path) -> list[dict[str, object]]:
    """Size and SHA-256 of every catalogued input, so the report names exactly what it read."""
    inputs: list[dict[str, object]] = []
    for spec in load_catalog(catalog):
        for rel in [f.path for f in spec.files] + ([spec.parameters] if spec.parameters else []):
            data = (catalog.parent / rel).read_bytes()
            inputs.append(
                {"file": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            )
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.gis.assess", description=__doc__.splitlines()[0]
    )
    parser.add_argument("catalog", type=Path, help="catalog.toml describing the input documents")
    parser.add_argument("--out", type=Path, required=True, help="output folder")
    parser.add_argument(
        "--municipality", default="podgorica", help="profile id (municipalities/<id>.toml)"
    )
    parser.add_argument("--no-previews", action="store_true", help="skip the layer-isolation PNGs")
    parser.add_argument(
        "--ogrinfo", help="path to GDAL's ogrinfo (default: PATH, $OGRINFO, dev bundle)"
    )
    parser.add_argument("--cache", type=Path, help="pickle of the inspection results")
    parser.add_argument(
        "--report-only", action="store_true", help="rebuild the report from --cache"
    )
    args = parser.parse_args(argv)
    if args.report_only and not (args.cache and args.cache.is_file()):
        parser.error("--report-only needs an existing --cache file")

    gis = load_gis_profile(args.municipality)
    if gis is None:
        parser.error(f"profile {args.municipality!r} has no [gis] table")
    started = time.monotonic()

    def progress(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {message}", flush=True)

    if args.report_only:
        assessment = pickle.loads(args.cache.read_bytes())  # noqa: S301 - our own cache file
        refresh(assessment, args.catalog)
    else:
        preview_dir = None if args.no_previews else args.out / "previews"
        assessment = run(args.catalog, gis, preview_dir, ogrinfo=args.ogrinfo, progress=progress)
        assessment.inputs = _inputs(args.catalog)
        if args.cache:
            args.cache.parent.mkdir(parents=True, exist_ok=True)
            args.cache.write_bytes(pickle.dumps(assessment))

    versions = {"pymupdf": pymupdf.VersionBind, "shapely": shapely.__version__}
    if assessment.gdal:
        versions["gdal"] = assessment.gdal.split(",")[0].replace("GDAL ", "")
    for path in write_all(assessment, args.out, assessment.inputs, versions):
        progress(f"wrote {path}")
    decisions = ", ".join(f"{d.spec.id}: {d.decision}" for d in assessment.documents if d.counted)
    progress(
        f"extraction {assessment.extraction_hours:g} h, "
        f"georeferencing {assessment.georef_hours:g} h; {decisions}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
