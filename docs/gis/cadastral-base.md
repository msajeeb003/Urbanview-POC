# Cadastral base loader

The "Cadastral parcels" layer (BRD §2.1, MUST) holds the existing cadastral parcels of Podgorica
with the cadastral municipality (KO) and the parcel number people search by. The BRD leaves the
method open ("API, export or scrape: availability and licensing to verify"). This loader
(`backend/core/cadastre/`, migration 0024) imports whatever a source agrees to provide, and nothing
else.

## Sources and access (status 2026-09-27)

| Source | Provides | Access | Import method |
|---|---|---|---|
| Geoportal UZN (webmap.uzn.me/geoportal01) | parcels, KO boundaries | **not confirmed** | an export UZN delivers, or its WFS once UZN confirms one |
| eMapa (emapa.me) | spatial / cadastral export | **not confirmed** | an export file |
| eKatastar (ekatastar.me) | ownership, legal burdens | **not confirmed** | a bulk attribute export delivered under an agreement only |

Access is configuration: `[cadastre.sources.<id>]` in `backend/municipalities/podgorica.toml`. An
import only runs with `access = "confirmed"`, `access_basis` (the agreement, written permission or
published open-data terms) and `licence_note`. Otherwise it stops and gives the reason
(`python -m core.cadastre check-access`). The loader has no scraping code and no scraping
fallback. It never queries the eKatastar web application, which is a per-parcel lookup that names
owners.

## Ownership and restitution: decision

- `public_ownership` and `restitution_or_legal_burden` are loaded **only** from a confirmed bulk
  eKatastar extract. A flag is set only when the extract states it explicitly (the values listed in
  `ownership_fields.true_values` / `false_values`). Nothing is derived from other data: not from
  owner names, and not from the absence of a burden.
- Until bulk access is confirmed in P0, both columns stay **null** ("not available", which is
  different from "no"). The API returns `null` for them; there are no ownership map layers in
  the POC (the flags ride on the `cadastral_parcels` tile features).
- The columns stay in the schema (nullable) rather than being dropped, so the API contract does not
  change when access is granted.
- If the flags are loaded later, they belong to the dataset that loaded them. Re-importing parcels
  without the eKatastar export clears them at the next publish, and the import report warns
  (`ownership_cleared`). Always import both together.

## Import

```
cd backend
python -m core.cadastre sources
python -m core.cadastre import --file ../data/cadastre/podgorica/uzn-export.zip \
    --retrieved-on 2026-10-02 [--layer NAME] [--ko-layer NAME] [--ownership flags.csv] [--store]
python -m core.cadastre datasets
```

1. **Acquire.** The adapter checks access and records the source, method, retrieval date, access
   basis, licence note and the file's SHA-256 and size. `--store` keeps a copy in the private
   bucket. A WFS source is first copied into a local GeoPackage, so every import is reproducible.
2. **Read.** ogr2ogr reads the export (Shapefile, zip, GeoPackage, GML, DXF, a WFS layer, CSV / XLSX
   for attributes), keeps the export's CRS as metadata, and reprojects to EPSG:4326. The profile's
   `source_crs` is used only when the export carries none. `transform` (or `--transform`) names the
   coordinate operation when a datum shift matters: the MGI 1901 → WGS 84 default is good to about
   10 m only. Fields are mapped by the profile (`fields`): KO name and / or code, number,
   sub-number (a number written "1234/5" is split), address. `ko_names` maps the source's KO
   spellings to the official names.
3. **Validate.** Invalid geometries are repaired with `ST_MakeValid` and counted. These are
   **errors**, and the dataset is recorded as `invalid` with nothing staged:
   - a record without a KO, a number or a polygon;
   - a geometry that cannot be repaired;
   - the same (KO, number, sub-number) twice (`on_duplicate = "merge"` unions parts of one parcel
     instead).

   These are **warnings**:
   - parcels outside the municipality's extent;
   - grid coverage of the extent below `min_coverage` (500 m cells);
   - KOs of the profile missing from the export;
   - parcels under 1 m²;
   - a CRS the export declares that differs from the profile's.
4. **Stage and diff.** The parcels become a `cadastral_parcels` batch and the KO boundaries a
   `cadastral_municipalities` batch: delivered, else derived from the parcels. Areas are computed
   in EPSG:25834. The diff against the previous version (the published one first) classifies every
   parcel: added, removed, geometry changed, attributes changed, unchanged. Parcels of KOs the new
   export does not cover are out of scope and never removed. A version that removes more than
   `mass_change_threshold` (20 %) of the previous parcels in its KOs is refused unless
   `--accept-large-change` (a renamed KO or a partial export would otherwise retire whole KOs).
   Reports: `report.md`, `report.json`, `diff.csv` in `data/cadastre/<m>/<version>/`
   (git-ignored).
5. **Review, then publish.** Both batches are staged with origin `official_gis` and their
   topology QA (validity and overlaps; the grid coverage is the import's own check), and wait in
   the console's geometry review for a reviewer's decision (publishing waits while they are
   pending). `POST /v1/admin/publish` then applies the approved dataset in the same transaction as
   everything else:
   - parcels are upserted by (KO, number, sub-number), so Parcel IDs stay stable;
   - parcels of the imported KOs that the new version no longer contains are **retired**
     (`retired_at`, `retired_dataset_version`), never deleted. Links of earlier versions and orders
     keep resolving, but nothing retired is served (lookup, map tiles, links, counts);
   - the KO table (`cadastral_municipalities`, `GET /v1/cadastral-municipalities`) is refreshed;
   - the dataset is marked published and the previous one superseded. Batches and dataset rows
     stay as history.

## Acceptance

| Criterion | Status |
|---|---|
| A parcel is found by KO + number and its polygon sits on the right spot | Proven on the sample extract (`tests/integration/test_cadastre_postgis.py`: "Test KO Alpha 1234/5" found by `GET /v1/locate/parcel`, area 600 m² as drawn in UTM 34N, centroid within 0.1 m of the source position). "KO Podgorica II, 1234/5" on real data waits for the export. |
| The full Podgorica extent imports and every geometry is valid | Blocked on bulk access (P0). The loader, validation and coverage check are ready. |
| Ownership / restitution decision recorded | Above. |

## Open with the client / UZN

- Bulk access and licence for the parcels (UZN export or WFS; eMapa as the alternative).
- The export's CRS (MONREF97 / EPSG:25834 or MGI Balkans zone 6 / EPSG:3908), and the official
  transformation if it is MGI.
- The official KO list with codes (replaces the profile's placeholder `cadastral_municipalities`).
- The export's field names (the profile's `fields` are placeholders; `--dry-run` shows what an
  export contains without staging it).
- Bulk access to eKatastar ownership and burden attributes, or confirmation that they stay out of
  the POC.
