# Information panel: `GET /v1/panel` — implementation contract (v2)

Status: engineering contract for the POC (2026-09-23, revised after design review). Product rules
come from `CLAUDE.md` and the BRD; formulas and Montenegrin labels are **pending client
validation** (P0 gate 3) and every payload says so (`formula_version = "poc-1"`,
`client_validated = false`).

The panel is what a visitor sees after selecting an object on the map. It is served **only from
the published serving tables**. Nothing from the review/staging table
(`planning_parameter_extractions`) is ever read by this endpoint: rejected or under-review values
are simply absent.

Decisions recorded for the product owner (see the end of this file for the open list): the
feasibility rows follow ticket B's list (GFA, coverage area, saleable area, construction cost,
revenue, profit, ROI) and the BRD Group 2 items not in that list (land value, design &
documentation costs) are served as first-class labelled cost rows next to them.

## 1. Endpoint

```
GET /v1/panel?type=zone|document|cadastral|urban&id=<int>
    [&saleable_share=0.70][&construction_cost_eur_m2=860][&sale_price_eur_m2=2450]
```

- `type` and `id` are required. Unknown `type` → 422 (`validation_error`). Unknown `id` for the
  type → **404** `not_found` (a missing *entity* looked up by primary key is an error; only
  uncovered *locations* are not). The frontend treats 404 as "this link is stale" and returns to
  the map, never a red state.
- The three assumption parameters are optional user overrides. They apply to every panel that
  carries a feasibility block (`urban`, and `cadastral` in both basis modes). Validation:
  `saleable_share` in (0, 1]; the two rates in (0, 100 000]. Out of range → 422.
- Every response carries `type`, `municipality_id`, `data_version`, `data_version_date`,
  `formula_version`, `client_validated: false`.
- One SQL statement per panel type (one round trip), same style as `api/services/locate_sql.py`
  (CTEs + `jsonb_build_object` / `jsonb_agg`, parameters cast explicitly), read from the serving
  tables only. The service caches nothing between requests (admin changes must show immediately).
- Performance: well inside the 2 s budget; integration tests assert execution time and index use.

Routing: `api/routers/v1/panel.py`, registered in `api/routers/v1/__init__.py`. Service:
`api/services/panel.py` (assembly) + `api/services/panel_sql.py` (statements) +
`api/services/panel_text.py` (bilingual labels and reason texts). Schemas: `api/schemas/panel.py`.
Engine: `core/engine/feasibility.py`.

**Alignment change to the locate endpoint:** `LocationResolution.calculation_basis` becomes
`"urban" | "cadastral"` (was `urban_parcel` / `cadastral_parcel`) so both endpoints share one
vocabulary. (Done by the orchestrator, including its tests.)

## 2. Schema additions (migration `0003_panel_schema`, models in `core/models/panel.py`)

All tables **except `planning_fields`** carry `municipality_id text NOT NULL` (indexed),
`dataset_version text NULL` (ingestion tag, same convention as the location tables; the seed
loader and `delete_dataset` rely on it) and `created_at timestamptz NOT NULL DEFAULT now()`.
Naming follows `core/models/planning.py`. Enums are created/dropped exactly like
`planning_document_status` in migration 0002 (`postgresql.ENUM(..., create_type=False)`, explicit
`create(checkfirst=True)` in upgrade, `drop(checkfirst=True)` in downgrade; model `Enum` with
`values_callable`). `alembic check` must stay clean: explicit `Index(...)` objects on the models
mirror every `op.create_index`; partial unique indexes use `postgresql_where=text("...")` (Alembic
1.20 does not compare the WHERE text, only key columns); no expression indexes are added by this
migration. CHECK constraints are declared in both model and migration (Alembic does not compare
them).

### 2.1 `publish_versions`
| column | type | notes |
|---|---|---|
| id | bigserial PK | |
| municipality_id | text NN | |
| label | text NN | e.g. `2026-09-22.1`; `UniqueConstraint("municipality_id", "label", name="uq_publish_versions_label")` in model and migration |
| published_at | timestamptz NN default now() | |
| published_by | text null | |
| formula_version | text NN default `poc-1` | |
| notes | text null | |
| is_current | bool NN `server_default=text("false")` | partial unique index `uq_publish_versions_current` on (municipality_id) WHERE is_current |
| dataset_version, created_at | | |

`data_version` = `label` of the current row; `data_version_date = (published_at AT TIME ZONE
'UTC')::date` as ISO string. Publishing replaces the serving rows and flips `is_current` in one
transaction; serving rows carry `publish_version_id` for provenance and are by definition the
current published set. With no current row: `data_version = "unpublished"`, `data_version_date =
null`, and the panel still renders whatever serving rows exist (they can only arrive through the
publish job). `data_version` covers planning values; market inputs carry their own `source_date`
(see 2.5).

### 2.2 `planning_fields` (field dictionary, product-wide, never in the seed loader)
| column | type | notes |
|---|---|---|
| key | text PK | snake_case |
| field_group | text NN | `planning` |
| sort_order | int NN | |
| label_en / label_me | text NN | ME labels are provisional (client to confirm) |
| abbreviation | text null | `IZ`, `II`, `BGP` |
| unit | text null | `%`, `m`, `m²` |
| value_type | text NN | `text` \| `number` |
| computed | bool NN default false | computed by the engine, never stored (the publish job rejects values for computed keys) |
| formula | text null | human-readable, for computed fields |

Seeded **by the migration** (bulk insert; downgrade deletes):

| sort | key | label_en | label_me | abbr | unit | type |
|---|---|---|---|---|---|---|
| 10 | land_use | Land use designation | Namjena površina | | | text |
| 20 | max_site_coverage_pct | Max site coverage | Maksimalni indeks zauzetosti | IZ | % | number |
| 30 | max_far | Max floor area ratio | Maksimalni indeks izgrađenosti | II | | number |
| 40 | max_height_m | Max building height | Maksimalna visina objekta | | m | number |
| 50 | max_floors | Max number of floors | Maksimalna spratnost | | | text |
| 60 | building_line_m | Building line (setback from public area) | Građevinska linija | | m | number |
| 70 | setback_neighbours_m | Min distance from neighbouring parcels | Minimalna udaljenost od susjednih parcela | | m | number |
| 80 | parking_requirement | Parking requirement | Parkiranje | | | text |
| 90 | min_green_area_pct | Min green area | Minimalna zelena površina | | % | number |
| 100 | planned_parcel_area_m2 | Planned parcel area (per plan) | Površina urbanističke parcele | | m² | number |
| 110 | utilities | Infrastructure utilities | Infrastrukturna opremljenost | | | text |
| 200 | max_gfa_m2 | Calculated max gross floor area | Maksimalna bruto građevinska površina | BGP | m² | number, computed, formula `max_far × basis_area_m2` |
| 210 | max_coverage_area_m2 | Max coverage area | Maksimalna površina pod objektom | | m² | number, computed, formula `max_site_coverage_pct / 100 × basis_area_m2` |

The first 11 are "the 11 fields" of the Group 1 ticket; the two computed ones follow them.

### 2.3 `planning_parameter_values` — SERVING (approved, published values only)
| column | type | notes |
|---|---|---|
| id | bigserial PK | |
| municipality_id | text NN | |
| document_id | bigint NN | FK planning_documents ON DELETE CASCADE; the document the value is cited from |
| urban_parcel_id | bigint null | FK urban_parcels ON DELETE CASCADE; **null = document-level value** (a provision the plan states for the whole document / zone; it is cited from its own page and applies to parcels under the document that have no parcel-level value, and to cadastral-basis panels) |
| field_key | text NN | FK planning_fields(key) |
| value_text | text null | for `text` fields |
| value_number | double precision null | for `number` fields |
| unit | text null | override of the dictionary unit, rarely needed |
| source_page | int **NN** | 1-based page in the source PDF (the publish job refuses rows without a page) |
| source_bbox | jsonb null | `[x0, y0, x1, y1]` in PDF points, origin bottom-left, on that page |
| source_note | text null | e.g. "table 3 – UP 12" |
| publish_version_id | bigint NN | FK publish_versions ON DELETE RESTRICT |
| dataset_version, created_at | | |

Constraints and indexes: `CheckConstraint("num_nonnulls(value_text, value_number) = 1",
name="ck_planning_parameter_values_one_value")`; composite FK `(urban_parcel_id, document_id)` →
`urban_parcels (id, document_id)` ON DELETE CASCADE named `fk_planning_parameter_values_parcel_document`
(requires `UniqueConstraint("id", "document_id", name="uq_urban_parcels_id_document")` on
`urban_parcels`, added by this migration) so a parcel-level row can only cite the parcel's own
document; `ix_planning_parameter_values_document_id`; `ix_planning_parameter_values_publish_version_id`;
partial unique `uq_planning_parameter_values_parcel` on (urban_parcel_id, field_key) WHERE
urban_parcel_id IS NOT NULL (it also serves parcel lookups: no separate index on urban_parcel_id,
model column `index=False`); partial unique `uq_planning_parameter_values_document` on
(document_id, field_key) WHERE urban_parcel_id IS NULL.

### 2.4 `planning_parameter_extractions` — STAGING (review queue; never read by the public API)
Explicit column list: id, municipality_id NN, document_id NN (FK planning_documents CASCADE),
urban_parcel_id null (FK urban_parcels CASCADE), field_key NN (FK planning_fields), value_text,
value_number, unit, source_page int **null**, source_bbox jsonb null, source_note, extracted_by
text NN (`llm:<model>` or `manual`), extracted_at timestamptz NN default now(), review_state
enum `review_state` (`pending_review`, `approved`, `rejected`, `amended`) NN default
`pending_review`, reviewer text null, reviewed_at timestamptz null, review_note text null,
published_value_id bigint null (FK planning_parameter_values ON DELETE SET NULL), dataset_version,
created_at. **No publish_version_id.** Index `ix_planning_parameter_extractions_review` on
(municipality_id, review_state); `ix_planning_parameter_extractions_document_id`. The publish
job copies approved rows into 2.3; this endpoint must not touch this table (a test proves a
pending and a rejected row never surface).

### 2.5 `financial_assumptions` — current admin market inputs per zone
| column | type | notes |
|---|---|---|
| id | bigserial PK | |
| municipality_id | text NN | |
| zone_id | bigint null | FK zones ON DELETE CASCADE; null = municipality-wide default |
| land_rate_eur_m2 | double NN | land value per m² of parcel area |
| build_rate_eur_m2 | double NN | construction cost per m² GFA |
| design_rate_eur_m2 | double NN | design & documentation per m² GFA |
| sale_rate_eur_m2 | double NN | selling price per m² saleable area |
| range_low_factor | double NN default 0.86 | multiplies a rate for its low bound |
| range_high_factor | double NN default 1.15 | multiplies a rate for its high bound |
| source | text null | e.g. `Realitica, Estitor, Monstat` |
| source_date | date null | |
| notes | text null | |
| is_current | bool NN `server_default=text("false")` | two partial unique indexes: `uq_financial_assumptions_current_zone` on (municipality_id, zone_id) WHERE `is_current AND zone_id IS NOT NULL`; `uq_financial_assumptions_current_default` on (municipality_id) WHERE `is_current AND zone_id IS NULL` |
| created_by | text null | |
| dataset_version, created_at | | |

`CheckConstraint("range_low_factor <= 1 AND range_high_factor >= 1 AND land_rate_eur_m2 > 0 AND
build_rate_eur_m2 > 0 AND design_rate_eur_m2 > 0 AND sale_rate_eur_m2 > 0",
name="ck_financial_assumptions_values")`. There is no per-zone saleable share: the client's 0.70 is
the product constant (engine default); precedence is query override > 0.70.

Decision (POC): market inputs are **admin-published directly** (making a row current is the
publish act, to be audit-logged by the admin track); they are not snapshotted per
`data_version`. The panel shows their `source`, `source_date` and `effective_from` (= created_at
of the current row) so a visitor can see how fresh the market data is.

Resolution for a panel: current row for the parcel's zone, else the municipality-wide current row
(zone_id null), else **no market data** → the feasibility fields that need rates are
`cannot_calculate` with reason code `no_market_data` (params `{zone_name}`), or
`no_market_data_zone_unknown` when the zone is null.

### 2.6 `planning_documents.amends_document_id` (new nullable column, FK planning_documents ON DELETE SET NULL, `ix_planning_documents_amends_document_id`)
Explicit "this document amends that one" link set at ingestion. Amendments are linked **by this
column only, never by coverage intersection**. Seed rows must be ordered so an amended document
precedes its amendment.

Models: add `PublishVersion`, `PlanningField`, `PlanningParameterValue`,
`PlanningParameterExtraction`, `FinancialAssumption` to `core/models/panel.py`, export from
`core/models/__init__.py`, add `amends_document_id` to `PlanningDocument` and the
`uq_urban_parcels_id_document` constraint to `UrbanParcel`.

## 3. Seeds (`database/seeds/podgorica_sample/`, loader `core/seeds.py`)

Loader changes: `TABLES: list[tuple[str, str | None, frozenset[str]]]` — `None` as geometry
column means "JSON rows file `<table>.json`" (a plain JSON array of row objects, `id` explicit).
Add a per-table column→PostgreSQL type map next to `TABLES` (e.g. `{"source_date": "date",
"published_at": "timestamptz", "extracted_at": "timestamptz", "reviewed_at": "timestamptz",
"source_bbox": "jsonb", "review_state": "review_state", "status": "planning_document_status"}`):
every parameter for a typed column is bound as `CAST(:col AS <type>)` and list/dict values are
`json.dumps`-ed before binding; untyped columns stay `:col`. Insert order (FK order):
zones, planning_documents (allowed set gains `amends_document_id`), urban_blocks, urban_parcels,
cadastral_parcels, publish_versions, financial_assumptions, planning_parameter_values,
planning_parameter_extractions. `replace` deletes in reverse order by municipality;
`load_synthetic_bulk` / `delete_dataset` keep iterating `TABLES` (all have `dataset_version`);
sequences re-synced for every table. `planning_fields` is not in `TABLES`.

Sample content (municipality `podgorica`):

- `publish_versions.json`: id 1, label `sample-2026-09-22`, published_at `2026-09-22T10:00:00Z`,
  published_by `seed`, formula_version `poc-1`, is_current true.
- `financial_assumptions.json`: id 1 zone 1 Centar: land 1350, build 860, design 90, sale 2450,
  factors 0.86 / 1.15, source "Realitica, Estitor, Monstat (sample)", source_date `2026-08-01`,
  is_current true; id 2 zone 2 Stari Aerodrom: land 900, build 780, design 90, sale 1650, same
  factors, is_current true. No municipality-wide default.
- `planning_documents.geojson`: document 3 gets `"amends_document_id": 2`.
- Split parcel (asymmetric so the primary is meaningful): cadastral **#1044**, id 1006, KO
  Podgorica I, street "Bulevar Save Kovačevića 16", polygon
  `[[19.2622,42.4416],[19.2630,42.4416],[19.2630,42.4419],[19.2622,42.4419]]`; urban parcels
  UP 31 (id 5) `[[19.2622,42.4416],[19.26265,42.4416],[19.26265,42.4419],[19.2622,42.4419]]`
  (≈ 56 % of #1044) and UP 32 (id 6) `[[19.26267,42.4416],[19.2630,42.4416],[19.2630,42.4419],[19.26267,42.4419]]`
  (≈ 41 %), both document 2, block 1. Primary = UP 31.
- Cadastral-basis parcel under the general plan: cadastral **#2002**, id 1007, KO Podgorica I,
  street null, polygon `[[19.2525,42.4365],[19.2530,42.4365],[19.2530,42.4368],[19.2525,42.4368]]`
  — covered only by document 1 (PUP), no urban parcel, no block.
- `planning_parameter_values.json` (all `publish_version_id: 1`; **every row has source_page**):
  - UP 12 (urban_parcel_id 1, document 2): all 11 fields stated. land_use "Residential – mixed
    use (ground-floor commercial)"; max_site_coverage_pct 55; max_far 3.2; max_height_m 27.5;
    max_floors "P+8"; building_line_m 5; setback_neighbours_m 4; parking_requirement "1 space per
    apartment + 1 per 60 m² commercial"; min_green_area_pct 20; planned_parcel_area_m2 959.6;
    utilities "water, sewage, electricity, district heating". Sources: document 2, pages 12–19,
    bboxes like `[72, 410, 520, 428]`, source_note "table 3 – UP 12".
  - UP 7 (id 3, document 4): all fields **except `max_height_m`** (ticket A acceptance). Values:
    land_use "Residential", coverage 40, far 2.4, floors "P+5+Pk", building line 5, setback 3,
    parking "1 per apartment", green 25, area 1370.9, utilities "water, sewage, electricity".
    Sources: document 4, pages 8–11, note "table 2 – UP 7".
  - UP 21 (id 4, document 2): land_use "Residential", coverage 45, floors "P+4", **no max_far**
    and no document-level FAR exists for document 2, so FAR is `not_stated` → GFA and its
    dependents cannot be computed while coverage area can. Sources: document 2, page 20.
  - UP 31 / UP 32 (ids 5 / 6, document 2): coverage 50, far 2.0, floors "P+4", land_use
    "Residential". Sources: document 2, page 21.
  - Document-level rows for document 2 (urban_parcel_id null), page 5, note "general provisions
    §3": min_green_area_pct 15, parking_requirement "1 space per apartment", utilities "water,
    sewage, electricity". (No FAR/coverage/height at document level for a DUP.)
  - Document-level rows for document 1 (PUP, urban_parcel_id null), page 3, note "zone-wide
    provisions": land_use "Mixed use", max_far 1.5, max_site_coverage_pct 40, max_floors "P+3",
    min_green_area_pct 20. These make the cadastral-basis panel of #2002 computable.
- `planning_parameter_extractions.json` (staging, must never surface): for UP 12 `max_far` = 9.9
  `pending_review`; for UP 12 `max_height_m` = 99 `rejected`; both document 2, extracted_by
  `llm:sample`, extracted_at `2026-09-20T09:00:00Z`.
- Integration conftest expectations become **7 cadastral parcels and 6 urban parcels**.

## 4. Feasibility engine (`core/engine/feasibility.py`, pure Python, deterministic)

Formula version `poc-1`. Inputs:

```python
MarketInputs(land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2, sale_rate_eur_m2,
             range_low_factor=0.86, range_high_factor=1.15)
Assumptions(saleable_share=0.70, construction_cost_eur_m2=None, sale_price_eur_m2=None)
compute_feasibility(basis_area_m2, max_far, max_site_coverage_pct, market, assumptions,
                    *, market_reason_code=None, market_reason_params=None) -> FeasibilityResult
```

Effective rates: `build = assumptions.construction_cost_eur_m2 or market.build_rate_eur_m2`,
`sale = assumptions.sale_price_eur_m2 or market.sale_rate_eur_m2`. User overrides do **not**
rescue a missing market row (land and design rates would be missing): with `market=None` the
four money fields stay `cannot_calculate`. Client formulas (CLAUDE.md):

| key | expected | low | high | range_kind |
|---|---|---|---|---|
| max_gfa_m2 | far × area | = expected | = expected | deterministic |
| max_coverage_area_m2 | coverage% / 100 × area | = | = | deterministic |
| saleable_area_m2 | gfa × saleable_share | = | = | deterministic |
| construction_cost_eur | gfa × build | gfa × build × lo | gfa × build × hi | range |
| revenue_eur (market value) | saleable × sale | × lo | × hi | range |
| profit_eur | revenue − total_cost | revenue_low − total_cost_high | revenue_high − total_cost_low | range |
| roi_pct | profit / total_cost × 100 | profit_low / total_cost_high × 100 | profit_high / total_cost_low × 100 | range |

Cost rows (same shape, `range`): `land_value_eur = area × land_rate`, `design_documentation_eur =
gfa × design_rate`, `construction_cost_eur` (as above), `total_cost_eur = land + design +
construction`; each low = × lo, high = × hi. Rounding: 2 decimals for areas, euros and ROI (see the implementation note below).
Ranges always satisfy low ≤ expected ≤ high.

Each field is `FieldRange(key, status="ok"|"cannot_calculate", reason_code, reason_params,
range_kind, low, expected, high)`; `low/expected/high` are numbers when ok and all null
otherwise. Reason codes (exact): `area_unknown`, `far_not_stated`, `coverage_not_stated`,
`requires_gfa`, `no_market_data` (params `{zone_name}`), `no_market_data_zone_unknown`,
`total_cost_zero`. Dependency rules: no area → all fields and cost rows cannot; no FAR → gfa cannot
and saleable/construction/revenue/profit/roi + design/total cannot (`requires_gfa`); no coverage
→ only coverage area cannot; no market → construction/revenue/profit/roi and all cost rows cannot
(gfa, coverage, saleable still ok); total_cost = 0 → roi cannot. Result exposes `fields` (7, fixed
order above), `cost_rows` (4, order above), `assumptions_used` (saleable_share,
construction_cost_eur_m2, sale_price_eur_m2, design_rate_eur_m2, land_rate_eur_m2,
range_low_factor, range_high_factor, sources: {construction_cost_eur_m2: "market"|"user"|null,
sale_price_eur_m2: "market"|"user"|null}) and `formula_version`, plus `to_dict()`.

Implementation note (2026-09-23): the engine is the shared TypeScript package
`packages/feasibility-engine` (engine keys `max_gfa`, `max_coverage_area`, `saleable_area`,
`construction_costs`, `land_value`, `design_and_documentation_costs`, `total_cost`,
`market_value`, `potential_profit`, `roi_pct`; money inputs given per m² or as totals, each with
admin-supplied multiplier or absolute bounds) and its Python copy `backend/core/engine/shared.py`;
`backend/core/engine/feasibility.py` is only the panel-facing adapter to the keys above. Both
engines are held to `packages/feasibility-engine/fixtures/feasibility-cases.json` with exact
equality (`backend/tests/test_feasibility_shared.py`, `packages/feasibility-engine/test`).
Rounding is 2 decimals for areas, euros and ROI (half away from zero on the shortest decimal
representation, outputs only). The fixture file stays `client_validated: false` until the client
confirms the numbers; never regenerate it from an engine.

## 5. Payloads

### 5.0 Conventions
- **Numbers**: the server sends raw JSON numbers, never formatted strings. `_pct` fields are
  0–100 with 1 decimal; `_share`, `_ratio` are 0–1; `_factor` are multipliers; areas 1 decimal;
  euros whole; the client formats per language.
- Common fields on every panel: `type`, `municipality_id`, `data_version`, `data_version_date`,
  `formula_version`, `client_validated: false`.
- `DocumentRef = {id, name, type, status, status_label_en, status_label_me, source, registry_url,
  amends_document_id}` (status labels: adopted → "adopted" / "usvojen", in_progress →
  "in progress" / "u izradi", superseded → "superseded" / "zamijenjen"; `registry_url` =
  `planning_documents.source_url`; `type` is the abbreviation shown as-is in both languages).
- `ZoneRef = {id, name}`; `BlockRef = {id, block_ref}`; entity ids are always `id` inside refs.
- `Header = {ko_and_number, zone: ZoneRef | null, documents: [DocumentRef + {role:
  "governing" | "amendment"}], data_version, data_version_date}` with `ko_and_number = "KO
  {ko_name}, {parcel_number}[/{sub_number}]"` (null when no cadastral parcel); documents:
  governing first, then its in-progress amendments name asc.
- `Areas = {cadastral_area_m2 | null, urban_parcel_area_m2 | null, delta_m2 | null,
  delta_pct | null, overlap_m2 | null, share_of_cadastral_pct | null, share_of_urban_pct | null,
  planned_area_stated_m2 | null, stated_vs_geometry_delta_pct | null, calculation_basis:
  "urban" | "cadastral", basis_area_m2, basis_reason_code, basis_reason_params, basis_reason_en,
  basis_reason_me}` with `delta_pct = (urban − cadastral) / cadastral × 100` (negative = planned
  parcel smaller), `basis_area_m2 = urban_parcels.area_m2` (geometry) for urban basis and
  `cadastral_parcels.area_m2` for cadastral basis; `planned_area_stated_m2` = the stated
  `planned_parcel_area_m2` value when present; `stated_vs_geometry_delta_pct` surfaces any
  difference (null when not stated). Basis reason codes: `urban_covers_cadastral` (params
  `{urban_parcel_number, cadastral_parcel_number, share_pct}`, en "planned parcel UP 12 covers
  70% of cadastral parcel 1042", me "urbanistička parcela UP 12 pokriva 70% katastarske parcele
  1042"), `no_urban_parcel` (params `{cadastral_parcel_number}`, en "no planned parcel is defined
  over cadastral parcel 2001/1; cadastral area used", me "nad katastarskom parcelom 2001/1 nije
  definisana urbanistička parcela; koristi se katastarska površina"), `no_cadastral_parcel`
  (params `{urban_parcel_number}`, en "planned parcel UP 21 has no cadastral parcel under it").
- Tier markers: `planning.tier = "free"`; `market_inputs.tier`, `assumptions.tier`,
  `feasibility.tier = "paid"`. The POC serves the paid blocks without entitlement checks; the
  marker exists so the boundary is explicit on screen and a later gate needs no redesign.

### 5.1 zone
```
{ type: "zone", zone: {id, name, general_planning_summary},
  header: {title: name, subtitle_en: "Internal city division", subtitle_me: "Interna podjela grada"},
  planning_documents: [DocumentRef],   // adopted first, then in progress, then superseded; name asc within status
  counts: {documents, adopted, in_progress, superseded} }
```
### 5.2 document
```
{ type: "document", document: DocumentRef + {ingestion_dataset_version},
  amendments_in_progress: [DocumentRef],   // amends_document_id = id AND status = in_progress, name asc
  zones: [ZoneRef],                        // zone_id ∪ zones whose geometry intersects coverage, name asc
  coverage_counts: {cadastral_parcels, urban_parcels},  // cadastral: ST_PointOnSurface(geom) within coverage; urban: document_id = id
  general_planning_summary: <zone.general_planning_summary of zone_id> | null }
```
### 5.3 cadastral
```
{ type: "cadastral",
  identification: {parcel_id, parcel_number, sub_number, ko_name, street_address,
                   urban_block: BlockRef | null,          // primary urban parcel's block, else block containing ST_PointOnSurface(geom)
                   cadastral_area_m2, governing_document: DocumentRef | null, zone: ZoneRef | null},
  header: Header,
  flags: {public_ownership: bool, restitution_or_legal_burden: bool,
          note_en: "false means not flagged in the cadastral extract", note_me: "false znači da nije označeno u katastarskom izvodu"},
  urban_parcel_defined: bool,
  urban_parcel: UrbanLink | null,          // primary: largest overlap, then smallest planned area, then lowest id (locate's rule)
  urban_parcels: [UrbanLink],              // all links ≥ thresholds, same order
  split: bool,                             // more than one link
  areas: Areas,                            // cadastral vs primary urban parcel; urban fields null when none
  calculation_basis: "urban" | "cadastral",
  basis_area_m2,
  planning: PlanningBlock,                 // basis urban: the PRIMARY urban parcel's block (one round trip renders both tickets; other links are opened via urban_parcels); basis cadastral: document-level values of the governing document; null only when there is no governing adopted document
  market_inputs: MarketInputsBlock | null, assumptions: AssumptionsBlock | null, feasibility: FeasibilityBlock | null,   // same condition
  covered: bool,                           // false when no adopted document governs the parcel → planning/market/assumptions/feasibility null
  centroid: {lat, lng}, geometry: GeoJSON }
UrbanLink = {id, urban_parcel_number, area_m2, overlap_m2, share_of_cadastral_pct, share_of_linked_pct,
             delta_pct,                    // (area_m2 − cadastral_area) / cadastral_area × 100
             document: DocumentRef, urban_block: BlockRef | null}
```
Links use the same overlap thresholds as locate (`LOCATE_MIN_OVERLAP_M2`,
`LOCATE_MIN_OVERLAP_FRACTION`); `share_of_linked_pct` values sum to 100 (± rounding). Governing
document = the adopted document with the most-specific coverage containing
`ST_PointOnSurface(geom)` (locate's rule); zone = governing document's zone, else the zone
containing that point.

### 5.4 urban
```
{ type: "urban",
  identification: {urban_parcel_id, urban_parcel_number, urban_block: BlockRef | null,
                   governing_document: DocumentRef,      // the parcel's document
                   zone: ZoneRef | null,                 // document.zone_id, else block.zone_id, else smallest zone intersecting ST_PointOnSurface(geom)
                   cadastral_parcel: CadastralLink | null,   // primary = largest overlap
                   linked_cadastral_parcels: [CadastralLink]},
  header: Header,
  areas: Areas,                            // calculation_basis always "urban"
  calculation_basis: "urban", basis_area_m2,
  covered: bool,                           // false when the parcel's document is not adopted → planning/market/assumptions/feasibility null, coverage_note_en/me
  planning: PlanningBlock | null, market_inputs: MarketInputsBlock | null,
  assumptions: AssumptionsBlock | null, feasibility: FeasibilityBlock | null,
  centroid, geometry }
CadastralLink = {parcel_id, parcel_number, sub_number, ko_name, street_address, area_m2, overlap_m2,
                 share_of_urban_pct, share_of_cadastral_pct}
```

### 5.5 Shared blocks
```
PlanningBlock = {
  tier: "free", calculation_basis, basis_area_m2,
  fields: [ PlanningField ],               // dictionary order; all 13 always present
  not_stated_label: {en: "not stated in plan", me: "nije navedeno u planu"} }
PlanningField = {key, label_en, label_me, abbreviation, unit,   // unit = row override else dictionary unit
  value_type, status: "stated" | "not_stated" | "computed" | "cannot_compute",
  value: number | string | null,           // null when not_stated / cannot_compute; a stated 0 is a real 0
  scope: "parcel" | "document" | null, fallback: bool,   // fallback = true when a document-level value is used for an urban parcel
  source: Source | null,                   // NON-NULL whenever status = stated; null otherwise
  formula: string | null, derived_from: [keys] | null,   // computed rows
  reason_code, reason_en, reason_me }      // cannot_compute (far_not_stated / coverage_not_stated / area_unknown)
Source = {document_id, document_name, page, bbox, bbox_space: "pdf-points-bottom-left", note,
          registry_url, value_id, viewer_url: "/v1/source/value/{value_id}"}  // section 10
MarketInputsBlock = {tier: "paid", available: bool, reason_code, reason_en, reason_me,
  zone: ZoneRef | null, land_rate_eur_m2, build_rate_eur_m2, design_rate_eur_m2, sale_rate_eur_m2,
  range_low_factor, range_high_factor, source, source_date, effective_from}   // numbers null when unavailable
AssumptionsBlock = {tier: "paid", saleable_share, construction_cost_eur_m2, sale_price_eur_m2,
  design_documentation_eur_m2, land_rate_eur_m2, range_low_factor, range_high_factor,
  overrides: {saleable_share: bool, construction_cost_eur_m2: bool, sale_price_eur_m2: bool},
  sources: {construction_cost_eur_m2: "market" | "user" | null, sale_price_eur_m2: "market" | "user" | null},
  market_source, market_source_date, formula_version, data_version, data_version_date, client_validated: false}
  // when market data is unavailable the rate numbers are null; saleable_share and the override flags still reflect the query
FeasibilityBlock = {
  tier: "paid", calculation_basis, basis_area_m2, formula_version,
  fields: [ FeasibilityField ×7 ],          // order: max_gfa_m2, max_coverage_area_m2, saleable_area_m2, construction_cost_eur, revenue_eur, profit_eur, roi_pct
  cost_rows: [ FeasibilityField ×4 ],       // land_value_eur, design_documentation_eur, construction_cost_eur, total_cost_eur
  disclaimer_en: "Figures are indicative ranges derived from the adopted plan and public market data, not investment, planning or legal advice.",
  disclaimer_me: "Iznosi su indikativni rasponi izvedeni iz usvojenog plana i javnih tržišnih podataka, a ne investicioni, planerski ni pravni savjet.",
  disclaimer_status: "placeholder" | "client_approved", disclaimer_version: "poc-1" }
FeasibilityField = {key, label_en, label_me, unit, range_kind: "deterministic" | "range",
  status: "ok" | "cannot_calculate", reason_code, reason_params, reason_en, reason_me,
  low, expected, high}                      // numbers when ok, all null otherwise
```
The two computed PlanningFields and the first two FeasibilityFields carry the same numbers.
Reason texts (en / me): area_unknown "parcel area unknown" / "površina parcele nije poznata";
far_not_stated "FAR not stated in plan" / "indeks izgrađenosti nije naveden u planu";
coverage_not_stated "site coverage not stated in plan" / "indeks zauzetosti nije naveden u planu";
requires_gfa "requires max GFA" / "zahtijeva maksimalnu BGP"; no_market_data "no market data
for zone {zone_name}" / "nema tržišnih podataka za zonu {zone_name}"; no_market_data_zone_unknown
"no market data (zone unknown)" / "nema tržišnih podataka (zona nepoznata)"; total_cost_zero
"total cost is zero" / "ukupni trošak je nula". Feasibility labels (en / me / unit): max_gfa_m2
"Max gross floor area" / "Maksimalna BGP" / m²; max_coverage_area_m2 "Max coverage area" /
"Maksimalna površina pod objektom" / m²; saleable_area_m2 "Saleable area" / "Prodajna površina" /
m²; construction_cost_eur "Construction cost" / "Troškovi izgradnje" / €; revenue_eur "Market
value (revenue)" / "Tržišna vrijednost" / €; profit_eur "Potential profit" / "Potencijalna dobit"
/ €; roi_pct "Return on investment" / "Povrat investicije" / %; land_value_eur "Estimated land
value" / "Procijenjena vrijednost zemljišta" / €; design_documentation_eur "Design and
documentation costs" / "Troškovi projektovanja i dokumentacije" / €; total_cost_eur "Total cost" /
"Ukupni trošak" / €.

Planning-field value resolution for an urban parcel: parcel-level value (`scope = "parcel"`),
else document-level value of the parcel's document (`scope = "document"`, `fallback = true`),
else `not_stated`. For a cadastral-basis panel: document-level values of the governing document
(`scope = "document"`, `fallback = false`). Computed fields use `basis_area_m2` and the resolved
`max_far` / `max_site_coverage_pct`.

## 6. Tests

- `backend/tests/test_feasibility.py`: fixture parity + reason/dependency rules + ranges ordered.
- `backend/tests/integration/test_panel_postgis.py` (marker `integration`):
  - zone 1: name, 3 documents with statuses (adopted first), counts {3, 2, 1, 0}, general
    summary, data_version `sample-2026-09-22`, data_version_date `2026-09-22`.
  - document 2: amendments_in_progress = [document 3], zones = [Centar], coverage_counts =
    {cadastral_parcels: 4 (#1042, #1043, #2001/1, #1044), urban_parcels: 5 (UP 12, 13, 21, 31, 32)},
    general summary. document 1: coverage_counts cadastral 5 (adds #2002), urban 0.
  - cadastral 1001 (#1042): urban_parcel UP 12, share_of_cadastral ≈ 70, delta ≈ −30, basis urban,
    planning = UP 12's block (11 stated), feasibility present and equal to the urban-1 panel's
    values, `covered` true. 1003 (#2001/1): `urban_parcel: null`, `urban_parcel_defined: false`,
    basis cadastral with document-2 document-level fields (min_green 15, parking, utilities
    stated; far/coverage not_stated) → max_gfa cannot_compute, feasibility gfa/… cannot,
    coverage area cannot. 1007 (#2002): basis cadastral under document 1 with land_use "Mixed use",
    far 1.5, coverage 40, floors "P+3", green 20 (scope document, fallback false) → feasibility
    fully computed on the cadastral area with Centar rates. 1006 (#1044): split = true, two
    links, primary UP 31, `share_of_linked_pct` sums to 100 ± 0.1, `share_of_cadastral_pct` ≈ 56
    and ≈ 41.
  - urban 1 (UP 12): header (KO Podgorica I, 1042; Centar; documents = DUP C2 governing +
    Izmjene amendment), areas with basis reason code `urban_covers_cadastral` and share ≈ 70,
    11 stated fields each with `source.page`, `document_name`, `registry_url`, computed
    max_gfa = 3.2 × 959.6 ≈ 3070.7 and coverage ≈ 527.8, market inputs from Centar, defaults
    (0.70, 860, 2450), 7 feasibility fields ok with low ≤ expected ≤ high (strict for `range`
    kinds) matching the engine fixture; cost_rows present; overrides via query change
    construction/sale and set `overrides` + `sources` flags; staging values (9.9 / 99) never
    appear; every stated field has a non-null source with a page.
  - urban 3 (UP 7): `max_height_m` status `not_stated`, value null, the other 10 stated; feasibility fully ok.
  - urban 4 (UP 21): `max_far` not_stated → max_gfa `cannot_compute` (`far_not_stated`),
    feasibility gfa/saleable/construction/revenue/profit/roi `cannot_calculate`, coverage area ok;
    min_green_area_pct / parking / utilities resolved with scope document, fallback true.
  - market data removed: `await pg_conn.execute(text("UPDATE financial_assumptions SET is_current
    = false WHERE zone_id = 1")); await pg_conn.commit()`, then a fresh app/client: urban 1 →
    construction/revenue/profit/roi `cannot_calculate` with reason_code `no_market_data`, params
    `{zone_name: "Centar"}`, en text "no market data for zone Centar"; gfa/coverage/saleable
    still ok; `market_inputs.available` false. Restore (`= true`, commit) in `finally`.
  - document not adopted: set document 4 status `superseded` (commit), fresh client: urban 3 →
    `covered` false, planning/feasibility null; restore in `finally`.
  - 404 for `id=999999` of each type; 422 for `type=foo`, missing id, `saleable_share=2`.
  - one statement per panel call (event listener on `pg_app.state.engine.sync_engine`);
    EXPLAIN ANALYZE execution < 250 ms for the cadastral and urban statements.
  - conftest sample counts updated (7 cadastral, 6 urban parcels); locate tests unchanged and green.

## 7. Documentation
- `CLAUDE.md`: add an "Information panel" section (serving vs staging rule, dictionary, data
  version, engine + fixtures, assumptions/overrides, amendments link, disclaimer placeholder).
- `database/README.md`: new tables; this file is the contract for the frontend tickets (36 source
  viewer, 39 order report, 42 analytics).

## 8. Open for the product owner
1. Ticket B's 7 feasibility rows differ from the BRD Group 2 list; both are served (see top).
2. Formulas, range model (rate × low/high factors) and Montenegrin labels/reasons await client
   validation; the disclaimer wording awaits the lawyer.
3. Market inputs are admin-published, not versioned with `data_version` (POC decision).
4. Free/paid: the paid blocks are served without entitlement checks in the POC, marked by `tier`.

## 9. `POST /v1/feasibility` — server-side recalculation (added 2026-09-23)

```
POST /v1/feasibility
{ "parcel_id": 1, "type": "urban" | "cadastral",
  "assumptions": { "construction_cost_per_m2"?: number, "selling_price_per_m2"?: number, "saleable_share"?: number } }
```

Same data path as the panel (one statement against the serving tables, the parcel's planning
parameters and its zone's current market row), the edits merged over the admin defaults, the same
shared engine. Validation: rates in (0, 100 000], `saleable_share` in (0, 1], unknown keys
rejected (422); unknown parcel → 404. Response: `type`, `parcel_id`, `municipality_id`,
`covered` (+ `coverage_note_en/me`), `calculation_basis`, `basis_area_m2`, `planning_inputs`
(plot area, FAR, coverage, computed GFA / coverage area), `feasibility` (the panel's
FeasibilityBlock: 7 fields incl. the explicit saleable area + 4 cost rows, each low / expected /
high), `assumptions` (the panel's AssumptionsBlock with `overrides` and `sources`), `engine`
(`@urbanview/feasibility-engine`, `engine_version`, `formula_version`, `range_derivation`,
`deterministic: true`), `data_version`, `data_version_date`, `client_validated: false`,
`disclaimer` {en, me, status, version}. An uncovered parcel returns 200 with `covered: false` and
null blocks. Deterministic: identical requests produce byte-identical responses; no AI on this
path. Tests: `backend/tests/integration/test_feasibility_route.py` (field-for-field equality with
the panel and with the shared fixtures).

## 10. Source viewer — `GET /v1/source/value/{value_id}`, `GET /v1/source/{document_id}/page/{page}` (added 2026-09-23)

One click from a planning value to the page it cites. Both routes answer:

```
SourcePage = {document_id, document_name, document_status, page, page_count: int | null,
  kind: "page_image" | "pdf_page",     // rendered PNG of the page, or the PDF with url ending in #page=N
  url,                                  // signed, short-lived URL into the private bucket (the only storage fact that leaves the API)
  content_type: "image/png" | "application/pdf", expires_at, expires_in_seconds,
  registry_url,                         // public registry page of the document
  value: SourceValue | null }           // only on /source/value/{value_id}
SourceValue = {value_id, field_key, label_en, label_me, value, unit, urban_parcel_id,
  bbox, bbox_space: "pdf-points-bottom-left", note}
```

- Schema: migration `0004_document_files` adds `planning_documents.file_key` (null = not stored),
  `page_count` (null = unknown) and `page_images_rendered` (default false); the ingestion job sets
  them. Existence is decided from these columns, never by probing the bucket.
- Errors: 404 `not_found` for an unknown document / value, a page beyond `page_count`, or a
  document without a file (`details.reason = "not_stored"`); 422 for non-positive ids / pages;
  503 when storage cannot sign or the planning database is absent.
- Headers: `Cache-Control: no-store`. Expiry: `SOURCE_URL_EXPIRES_SECONDS` (60–86400, default 900).
- Analytics: the client emits `source_reference_opened` with `document_id` + `page` from the body.
- Sample data: `make seed` uploads placeholder PDFs (`core.seeds.placeholder_pdf`) for documents
  1, 2, 4, 5; document 3 (in-progress amendment) has no file and answers 404 `not_stored`.

## 11. Assumption versions, per-rate ranges, zone typical parameters (added 2026-09-24)

- `MarketInputsBlock` gains `version: {id, version, zone_id, effective_from} | null` (the
  `financial_assumptions` row that produced the figures) and `ranges: {land_rate, build_rate,
  design_rate, sale_rate}` with `RateRange = {expected, low, high, kind: "absolute" | "multiplier"}`.
  `AssumptionsBlock` gains `market_version` (same object); `POST /v1/feasibility` echoes it.
- `ZonePanel` gains `typical_parameters: {id, version, land_use, max_far, max_site_coverage_pct,
  max_height_m, max_floors, notes, source: {document_id, document_name, page, note, registry_url} | null,
  verified_on, verified_by, note_en, note_me} | null` — the zone's current parameter set
  (staff-maintained, `GET/POST/PUT/DELETE /v1/admin/zone-parameters`).
- Schema: migration `0007_admin_config` (assumption `version` / `supersedes_id` / `retired_*`,
  absolute bound columns per rate with `low ≤ rate ≤ high`, table `zone_parameter_sets`).

## 12. Publish versions and the tiles pointer — `GET /v1/tiles/current` (added 2026-09-25)

Every planning value the panel shows belongs to the **current publish version**
(`data_version`); a publish creates a complete new set and flips the pointer, a rollback flips it
back. With no current version the panel says `data_version: "unpublished"` and every planning
field is `not_stated`.

`GET /v1/tiles/current` (public, `Cache-Control: no-store`):

```json
{
  "status": "published",
  "version_id": 7,
  "data_version": "2026-09-25.1",
  "published_at": "2026-09-25T09:12:03Z",
  "archive_url": "https://…/podgorica/tiles/7/2026-09-25.1.pmtiles?X-Amz-…",
  "expires_at": "2026-09-25T10:12:03Z",
  "layers": [
    {"id": "zones", "geometry_type": "polygon", "min_zoom": 8, "max_zoom": 16, "features": 3},
    {"id": "urban_parcels", "geometry_type": "polygon", "min_zoom": 13, "max_zoom": 16, "features": 10006}
  ],
  "min_zoom": 8,
  "max_zoom": 16
}
```

- `archive_url` is a signed URL into the private bucket; the PMTiles client reads it with HTTP
  range requests. Fetch a fresh one after `expires_at` (or on a 403).
- `status: "unpublished"` (no version yet) or a version without an archive (the seeded sample)
  answers `archive_url: null`; the map then shows the base map only.
- Source layers (one per map layer, toggled independently): `zones`, `document_coverage`,
  `urban_blocks`, `urban_parcels` (properties: parcel number, area, block, document, effective
  `max_far`, `max_site_coverage_pct`, `max_height_m`, `max_floors`, `land_use`, `max_gfa_m2`),
  `cadastral_parcels` (number, sub-number, KO, address, area, ownership and burden flags,
  `has_urban_parcel`, `primary_urban_parcel_id`, `overlap_fraction`, `area_delta_m2`),
  `public_ownership`, `legal_burdens`, `land_use`, `traffic_network`, `block_cells` and
  `zone_cells` (heatmap values: `max_site_coverage_pct`, `max_height_m`, `max_far`,
  `max_gfa_m2`, `saleable_area_m2`, `sale_rate_eur_m2`, `market_value_eur`, `price_band` 1–3).
  Feature `id` = the entity id (cadastral `id` is the Parcel ID; cells carry the block / zone
  id), so a click can go straight to `GET /v1/panel?type=…&id=…`.

## 13. Display-shaped panels — `GET /v1/parcels/{id}/panel`, `GET /v1/zones/{id}/panel` (added 2026-09-26)

Everything the panel shows for a cadastral parcel in one response, in display order, labels in
English and Montenegrin on every item (the frontend hard-codes none). Numbers are raw JSON numbers
(areas one decimal, `_pct` 0–100, `_share` 0–1, money as the engine rounds it).

```json
{
  "type": "parcel", "municipality_id": "podgorica", "parcel_id": 1001,
  "version_id": 1, "data_version": "sample-2026-09-22", "data_version_date": "2026-09-22",
  "formula_version": "poc-1", "client_validated": false,
  "covered": true, "coverage_note_en": null, "coverage_note_me": null,
  "header": {
    "parcel_id": 1001, "ko": "Podgorica I", "parcel_number": "1042", "sub_number": null,
    "title": "KO Podgorica I, 1042", "street_address": "…",
    "zone": {"id": 1, "name": "Centar"}, "urban_block": {"id": 1, "block_ref": "C2-01"},
    "documents": [
      {"id": 2, "name": "DUP Centar – Zona C2", "status": "adopted", "status_label_en": "adopted",
       "status_label_me": "usvojen", "role": "governing", "…": "…"},
      {"id": 3, "status": "in_progress", "role": "amendment", "…": "…"}
    ],
    "flags": [{"key": "public_ownership", "value": false, "label_en": "Public ownership", "label_me": "Javna svojina"}],
    "areas": {"cadastral_m2": 1370.9, "planned_m2": 959.6, "linked_planned_total_m2": null,
              "planned_stated_m2": 959.6, "delta_m2": -411.3, "delta_pct": -30.0,
              "mismatch": true, "note_en": "Planned area 959.6 m² vs cadastral area 1370.9 m²: …", "note_me": "…"},
    "calculation_basis": {
      "basis": "urban", "area_m2": 959.6, "reason": "planned_parcel",
      "explanation_en": "Calculations use planned urban parcel UP 12 (959.6 m²), which covers 70% of this cadastral parcel (1370.9 m²): …",
      "explanation_me": "…", "split": false, "links_source": "parcel_links",
      "links": [{"urban_parcel_id": 1, "urban_parcel_number": "UP 12", "document": {"…": "…"},
                 "urban_block": {"id": 1, "block_ref": "C2-01"}, "area_m2": 959.6, "overlap_m2": 959.6,
                 "overlap_pct": 70.0, "area_delta_m2": -411.3, "rank": 1, "primary": true}]
    }
  },
  "group1": {
    "tier": "free", "title_en": "Planning parameters", "title_me": "Planski parametri",
    "document": {"id": 2, "…": "…"}, "urban_parcel_number": "UP 12",
    "fields": [
      {"key": "max_far", "label_en": "Max floor area ratio", "label_me": "Maksimalni indeks izgrađenosti",
       "abbreviation": "II", "unit": null, "value_type": "number", "status": "stated", "value": 3.2,
       "scope": "parcel", "reason": null,
       "source": {"document_id": 2, "document": "DUP Centar – Zona C2", "page": 13,
                  "bbox": [72, 388, 520, 406], "bbox_space": "pdf-points-bottom-left", "file_id": null,
                  "note": "table 3 – UP 12", "registry_url": "…", "value_id": 3,
                  "viewer_url": "/v1/source/value/3"}},
      {"key": "max_height_m", "status": "not_stated", "value": null, "source": null,
       "reason": "not_in_document", "reason_en": "not stated in the planning document",
       "reason_me": "nije navedeno u planskom dokumentu", "…": "…"}
    ],
    "computed": [{"key": "max_gfa_m2", "status": "computed", "value": 3070.72,
                  "formula": "max_far × basis_area_m2",
                  "inputs": [{"key": "max_far", "value": 3.2}, {"key": "basis_area_m2", "value": 959.6}]}]
  },
  "market": {"tier": "paid", "scope": "zone", "zone": {"id": 1, "name": "Centar"},
             "label_en": "Selling price per m²", "unit": "€/m²",
             "sale_price_eur_m2": {"low": 2107.0, "expected": 2450.0, "high": 2817.5, "kind": "multiplier"},
             "source": "Realitica, Estitor, Monstat (sample)", "source_date": "2026-08-01",
             "effective_from": "…", "version": {"id": 1, "version": 1, "zone_id": 1, "effective_from": "…"}},
  "assumptions": {"tier": "paid", "formula_version": "poc-1", "client_validated": false,
                  "market_version": {"…": "…"},
                  "items": [
                    {"key": "construction_cost_eur_m2", "unit": "€/m²", "value": 860, "low": 739.6, "high": 989.0,
                     "source": "market", "editable": true, "engine_edit_key": "construction_cost_per_m2"},
                    {"key": "saleable_share", "unit": "share", "value": 0.7, "source": "product_default",
                     "editable": true, "engine_edit_key": "saleable_share"},
                    {"key": "sale_price_eur_m2", "editable": true, "engine_edit_key": "market_value_per_m2", "…": "…"},
                    {"key": "land_value_eur_m2", "editable": false, "…": "…"},
                    {"key": "design_documentation_eur_m2", "editable": false, "…": "…"}]},
  "group2": {"tier": "paid", "status": "ok", "calculation_basis": "urban", "basis_area_m2": 959.6,
             "formula_version": "poc-1",
             "fields": [{"key": "land_value_eur", "engine_key": "land_value", "unit": "€",
                         "status": "ok", "range_kind": "range", "low": …, "expected": …, "high": …}],
             "input_flags": [],
             "disclaimer_en": "…", "disclaimer_status": "placeholder", "disclaimer_version": "poc-1"},
  "engine": {"engine_version": "1.0.0", "formula_version": "poc-1",
             "range_derivation": "pessimistic-pairing-v1", "deterministic": true,
             "inputs": {"planning": {"plot_area": 959.6, "calculation_basis": "urban", "far": 3.2,
                                     "site_coverage_pct": 55}, "market": {"…": "…"},
                        "assumptions": {"saleable_share": 0.7}},
             "edit_keys": {"construction_cost_eur_m2": "construction_cost_per_m2",
                           "saleable_share": "saleable_share", "sale_price_eur_m2": "market_value_per_m2"}},
  "centroid": {"lat": 42.44, "lng": 19.26}, "bbox": [19.26, 42.44, 19.27, 42.45]
}
```

- **Group 1** lists the 11 stored fields in dictionary order. A value resolves parcel → block →
  zone → document and always has a `source`; `viewer_url` answers a signed link to the cited
  page (section 10). A null value always has a `reason`: `not_in_document`, `rejected` (the
  expert rejected the extracted value and nothing replaced it) or `unpublished`. Never a default.
- **Calculation basis:** `planned_parcel` (one linked planned parcel), `split` (several: the
  largest overlap is the basis, the others are listed with their own figures one click away via
  `GET /v1/panel?type=urban&id=`), `no_planned_parcel` (cadastral area with the governing
  document's general values), `not_covered`, `unpublished`. Links come from `parcel_links` of
  the current publish version.
- **Group 2** is exactly `calculate(engine.inputs)` of the shared engine, mapped by `engine_key`.
  When a visitor edits an assumption the browser runs `recalculate(engine.inputs, edits)` with the
  `engine_edit_key` of each edited item; no server round trip is needed. `input_flags` name the
  Group 1 inputs that are missing and what they affect (max height and floors are not formula
  inputs: the figures still compute, the flag says the height rule was not verified).
- **Uncovered parcel:** 200 with `covered: false`, a neutral note, `group1`, `market`,
  `assumptions`, `group2` and `engine` null, the header still filled. Unknown id: 404.
- **Caching:** the response carries a strong `ETag` and `Cache-Control: no-cache`; send
  `If-None-Match` to get 304 while nothing changed. `X-Panel-Cache` says hit / miss / bypass /
  revalidated. Any publish, rollback, coverage switch, document registration, assumption or zone
  parameter change produces a new ETag.

`GET /v1/zones/{id}/panel`:

```json
{
  "type": "zone", "municipality_id": "podgorica", "zone_id": 1, "version_id": 1,
  "data_version": "sample-2026-09-22", "data_version_date": "2026-09-22",
  "title": "Centar", "subtitle_en": "Internal city division", "subtitle_me": "Interna podjela grada",
  "zone": {"id": 1, "name": "Centar"},
  "summary": "…", "summary_label_en": "General planning summary", "summary_label_me": "Opšti planski sažetak",
  "documents": [{"id": 2, "name": "DUP Centar – Zona C2", "type": "DUP",
                 "type_name": "Detaljni urbanistički plan (detailed urban plan)", "status": "adopted",
                 "status_label_en": "adopted", "status_label_me": "usvojen", "covered": true,
                 "file_available": true, "registry_url": "…", "amends_document_id": null}],
  "counts": {"documents": 3, "adopted": 2, "in_progress": 1, "superseded": 0},
  "typical_parameters": {"…": "section 11"}
}
```

Documents are the zone's current versions: adopted first, then in progress, then superseded,
name order within a status. Same caching headers as the parcel panel.

## 14. Zone and document panels for the public map's S3 variants (added 2026-09-28)

`GET /v1/panel?type=zone` and `?type=document` gain what the wireframe's zone and document
panels show (migration `0016_document_adopted_on`):

- `DocumentRef.adopted_on: date | null` everywhere (adoption date, entered at registration).
- Zone panel: `zone.zone_type`; `planning_documents` = the zone's **current** versions only, each
  a `ZonePlanningDocument` = `DocumentRef + {covered, file_available, parcel_count}` (`covered` =
  adopted AND live AND current AND a coverage geometry; `parcel_count` = cadastral parcels whose
  point on surface lies in the coverage, null when not covered); `counts.covered`.
- Document panel: `document.file_available`; `zones[]` = `{id, name, zone_type, typical:
  {land_use, max_far, max_site_coverage_pct, max_height_m, max_floors} | null}` (the zone's
  current parameter set).
- Profile: `terminology.document_types_en` (English names shown after the abbreviation).
- Cadastral and urban panels: `engine = {engine_version, formula_version, range_derivation,
  deterministic, inputs, edit_keys, field_keys} | null` — the shared engine's exact inputs behind
  `feasibility`, for the browser's `recalculate(inputs, edits)` (added with the assumption
  sandbox).

