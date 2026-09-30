# AI document extraction: the canonical contract (schema 1.0, prompts 1.1)

Status: engineering contract for the POC (2026-09-26). Code: `backend/core/extraction/`. The
extraction job (`jobs/tasks/extraction.py`, next AI-track item) fills this contract; the review
queue and the publish job consume it.

AI has one job: **read the planning documents so people do not transcribe them.** It structures
what a document states against the document / block / urban parcel model and attaches a source
reference to every value. It **never invents a planning value and never does arithmetic** (not
even a unit conversion): the model transcribes, deterministic code types, normalises and checks.
Nothing it produces reaches the map except through 100 % expert review and the publish job.

## 1. Flow

```
PDF pages (text, words + boxes, table grids)          core.extraction.preprocess / chunking
                                                       (docs/specs/pdf-preprocessing.md)
  -> prompt: rules + glossary | task + document | pages   core.extraction.prompts (PROMPT_VERSION)
  -> model: structured output = transcription only       core.extraction.llm / response
  -> validation: every value found on its cited page      core.extraction.validate
  -> canonical ExtractionResult (SCHEMA_VERSION)          core.extraction.schema
  -> review-queue rows, pending_review, payload stored    core.extraction.staging
```

`core.extraction.run.run_task` is one request end to end (pure apart from the model call).

## 2. Leaves

Every field of every entity is a leaf, one of two shapes:

**Stated** (the document states it):

| key | meaning |
|---|---|
| `value` | a number in the field's canonical unit, or the document's own wording (never rephrased) |
| `unit` | canonical unit: `%`, `m`, `m²`, `ha` or null (FAR, text) |
| `raw_text` | the page text it was read from, **taken from the page** (whitespace collapsed), never the model's copy |
| `source` | `document_id`, 1-based `page`, `bbox` [x0, y0, x1, y1] in PDF points origin bottom-left (null when the text repeats and no row / column settles it), `table_ref` {table, row, column, cell} |
| `confidence` | 0–1, the model's |
| `extraction_method` | `text` \| `table` (the schema's `ocr` value is never set: there is no OCR, scanned pages stay unread) |
| `stated` | `{value, unit}` exactly as printed and as the document expresses it, before normalisation |
| `normalisation` | rules applied, in order (`decimal_comma`, `grouped_number`, `ratio_to_percent`, `ha_to_m2`, `m2_to_ha`, `date_dmy`) |
| `derived` | max floors only: `{notation, below_ground, above_ground, attic}` parsed by code |
| `category` | land use only: the UrbanView land-use class |
| `flags` | what the reviewer should look at (section 5); flags never remove a value |

**Missing**: `value: null` and `reason`:
- `not_found`: the pages read do not state it;
- `deferred`: the document leaves it to a later decision ("definisaće se konkursnim rješenjem"),
  with the deferring `raw_text` and its `source`;
- `unverified`: the model returned a value whose text is not on the cited page, or whose text
  does not contain the value. The model's candidate is kept in the result's `issues`.

## 3. Entities (`ExtractionResult`)

`schema_version`, `prompt_version`, `task`, `municipality_id`, `document_id`, `pages`, `model`,
`issues`, `stats`, and:

| entity | fields |
|---|---|
| `document` (planning document) | `name`, `document_type` (code: a key of the profile's `document_types`), `status` (code: adopted / in_progress / superseded, with the evidence as raw text), `gazette_reference`, `decision_number`, `decision_date` (ISO), `area_ha`, `amendments` [{relation amends / amended_by, document_name}], `rules` (limits for the whole area), `notes` |
| `blocks` (the plan's blocks or zones: "Blok A", "Zona B"; never UrbanView zones) | `block_ref`, `block_key`, `total_row` (a table's totals row: sums, not rules), `area_m2`, `rules`, `notes` |
| `urban_parcels` | `urban_parcel_number` (as printed: UP 12, A116/1), `parcel_key` (lookup key), `block_ref`, `planned_parcel_area_m2`, `rules`, `public_area_relation`, `other_conditions` |
| `utilities` (infrastructure, methodology step 2d) | `scope` {level document / block / urban_parcel, ref}, `kind` (water_supply, sewerage, stormwater, electricity, heating, gas, telecom, access_road, other), `status` (existing / planned / unknown), `description` |
| `land_use_legend` | `code`, `name`, `category` |
| `table_columns` | per table column: header as printed, page, the field it maps to |

`rules` = the Group 1 fields of `planning_fields` except the parcel area: `land_use`,
`max_site_coverage_pct` (IZ), `max_far` (II), `max_height_m`, `max_floors` (local notation kept,
`derived` counts), `building_line_m` (distance from the regulation line / public area),
`setback_neighbours_m`, `parking_requirement`, `min_green_area_pct`, `utilities`. Every field is
always present: stated or missing with a reason.

**Land-use classes** (product-wide `LandUseClass`): residential, residential_mixed, mixed_use,
central_activities, tourism, education_social, health, culture, religious, sport_recreation,
green_space, traffic_infrastructure, utility_infrastructure, industry, special_purpose, other. The
document's wording stays the value; the class comes from the profile's `land_use_terms` (first
match on accent-folded text) or, for a legend code such as `SS`, from the document's legend
(`legend_map` of a `land_use_legend` run). No class -> flag `land_use_unmapped`.

**Document status**: adopted / in_progress / superseded (the product vocabulary). Registration
(`planning_documents.status`) stays the authority; the extracted status is evidence for the
reviewer.

## 4. Units and normalisation (`fields.py`, `normalise.py`)

The model states how a value is printed (`m`, `m2`, `ha`, `percent`, `ratio` = an index printed
as a plain decimal, `none`); a printed sign (`40 %`, `3.5 m`, `12,4 ha`) wins over it. Code
converts, in `Decimal`, only:

| field | canonical | conversions / assumptions |
|---|---|---|
| `max_site_coverage_pct`, `min_green_area_pct` | % | ratio ≤ 1 × 100 (`ratio_to_percent`); a bare number is assumed ratio (≤ 1) or percent (> 1), flagged `unit_assumed`; a ratio > 1 is `unit_unexpected` |
| `max_far` | none | ratio or bare number taken as is |
| `planned_parcel_area_m2`, block `area_m2` | m² | ha × 10 000 (`ha_to_m2`); bare number assumed m² (`unit_assumed`) |
| `area_ha` | ha | m² / 10 000 (`m2_to_ha`); bare number assumed ha |
| `max_height_m`, `building_line_m`, `setback_neighbours_m` | m | bare number assumed m |
| `max_floors` | text | the notation as printed; `derived` from the profile's tokens (`Po`, `S`, `G` below ground; `P`, `VP` ground; digits = upper floors; `M`, `Pk`, `Ps` attic); unknown tokens -> `floors_not_derivable` |
| `decision_date` | ISO date | `d.m.yyyy` (`date_dmy`); otherwise kept as printed, `date_not_parsed` |

Numbers are parsed in the document's conventions (`0,4`, `1.906,09`, a wrapped cell `5705.1 7`);
`1.500` (decimal or thousands?) is flagged `number_ambiguous`. Words in a number field (`h/2`)
stay text, flagged `text_for_numeric_field`.

**Range sanity** (kept, flagged `out_of_range`): IZ and green area 0–100 %, FAR 0–20, parcel
area > 0 and ≤ 10 km², height 0–300 m (exclusive 0), distances 0–200 m, floors ≤ 60 above and
≤ 10 below ground, document area 0–100 000 ha.

## 5. Validation (`validate.py`)

Per field: shape (a value needs raw text and a page; a null needs a reason) -> a citation of the
empty cell of a row a merged value spans is resolved to the cell the value is printed in -> the raw
text must be in the cited grid cell (tried first: a short value such as a block letter is otherwise
matched to its first occurrence anywhere on the page, and a cell wrapped over several lines is
interleaved with its neighbours in the page text) or on the cited page (case-, accent-, quote- and
whitespace-insensitive, whole words, so `1` is not found inside `1906.09`) or on exactly one other
page read (`page_corrected`) -> the value must be inside that text -> typing and normalisation ->
box, method, confidence.

| flag | meaning |
|---|---|
| `low_confidence` | confidence below `EXTRACTION_LOW_CONFIDENCE` (default 0.7): still queued |
| `out_of_range`, `unit_assumed`, `unit_unexpected`, `number_ambiguous`, `text_for_numeric_field`, `floors_not_derivable`, `date_not_parsed`, `unknown_code`, `land_use_unmapped` | section 4 |
| `page_corrected` | the model cited another page; the text is on this one |
| `bbox_ambiguous` | the text repeats on the page and no row label / column header settles which: no highlight rather than a wrong one |
| `cell_mismatch` | the cited grid cell does not hold the value (the cell is dropped) |
| `aggregate_row` | read from a totals row (block sums), not a rule |
| `found_under_other_parcel` | prose conditions: the value's text sits only under another parcel's heading (a real value put on the wrong parcel passes grounding; this points at it) |

Issues (`malformed_leaf`, `page_not_in_input`, `raw_text_not_on_page`, `value_not_in_raw_text`,
`parcel_without_number`, `duplicate_parcel`) carry the model's candidate. **Nothing is dropped
silently and nothing is published from here.**

## 6. Prompts (`prompt_sets/v1.1/`, `PROMPT_VERSION = "1.1"`)

1.1 asks for compact answers (`compact.py`): per entity only the fields the pages state, each a
flat entry (value, unit as printed, text, page, table, cell, column, confidence, status stated |
deferred) without unions or nullable members; `to_legacy()` turns an answer into the full
response models the validator reads (`response.RESPONSE_MODELS`), so validation and the canonical
schema are unchanged. 1.1 is the only prompt set: 1.0 (a nested schema the API refused as too
complex) was deleted on 2026-10-01.

`manifest.toml` (prompt version, target schema version, one entry per task with its response
model), Jinja2 templates: `system.md` (the rules: transcribe never interpret, cite page and raw
text, never guess, deferred values, no arithmetic, units as printed, codes, confidence,
completeness; then the glossary), `fields.md` (field guide), `tasks/*.md` (document, block,
urban_parcel, parameter_table keyed by urban parcel number, infrastructure, land_use_legend),
`context.md` (the document being read), `user.md` (the pages), and `responses/*.schema.json`,
the structured-output schemas exactly as sent (`python -m core.extraction export`; a test keeps
them current).

- **Place-neutral templates**: the municipality, the documents' language, the planning glossary
  (IZ, II, UP vs cadastral parcel, KO, Blok / Zona, namjena, spratnost and its tokens, BRGP,
  građevinska / regulaciona linija, parking, utilities, adoption terms, deferral phrases, totals
  rows) and the document types come from `[terminology]` / `[extraction]` of
  `municipalities/<id>.toml` (BRD §8): the terminology is Podgorica's profile data, not template
  text.
- **Request**: two cached system blocks (rules + glossary per municipality; task + field guide +
  document per document) and the pages as the user message; `output_config.format` = the
  response schema (all objects closed, all properties required; bounds are checked in code).
- A changed prompt is a new directory (`v1.1`); every item records the version that produced it.

**Response (what the validator reads, after `to_legacy()`)**: per field `{value (string, as printed), unit, raw_text,
page, table_ref, confidence, absent_reason}`, the model never types or converts a number.

## 7. Model (`llm.py`)

`ClaudeModel`: Anthropic SDK (`ai` extra; the worker image installs it), streaming,
`output_config` {format, effort}, adaptive thinking. The default model is **Claude Sonnet 5** (2026-09-26: Opus is not
required): it reads the tables well, supports adaptive thinking and effort, and caches the system
prompt; Haiku 4.5 has no adaptive thinking or effort and caches only prompts of 4096+ tokens.
Settings: `ANTHROPIC_API_KEY` (else the SDK's own lookup), `EXTRACTION_MODEL`
(`claude-sonnet-5`; `claude-opus-5` for the hardest pages), `EXTRACTION_EFFORT` (high),
`EXTRACTION_ADAPTIVE_THINKING`, `EXTRACTION_MAX_TOKENS` (32 000), `EXTRACTION_TIMEOUT_SECONDS`, `EXTRACTION_LOW_CONFIDENCE`. Failures: `ModelUnavailable` /
`ModelRateLimited` (timeouts, 5xx / 529, 429: the job retries), `ModelRefused`,
`ModelOutputInvalid` (truncated or off-schema), `ModelError` (final). `ModelUsage` gives the
token counts for `jobs.cost.cost_for`.

## 8. Review queue rows (`staging.py`, migration 0017)

Only stated values of planning fields become `planning_parameter_extractions` rows, all
`pending_review`: parcel values on their urban parcel (looked up by `parcel_key` within the
document), block values on their block (`block_key`), document rules at document scope. Columns:
the value (`value_number` or `value_text`), canonical `unit`, `raw_text`, `confidence`,
`source_page`, `source_bbox`, `source_note` ("UP 12 – Indeks zauzetosti"), `extracted_by`
(`llm:<model>`), and new in 0017: `schema_version`, `prompt_version`, `extraction_method`,
`flags` (JSONB, default `[]`), `payload` (the leaf with its context, `StagedPayload`), `job_id`.
Everything else is reported in the staging plan, not dropped: non-planning fields (document
identity, block areas, public-area relation, other conditions, utilities, legend: they stay in the
extraction result), values whose parcel / block has no geometry yet, missing counts by reason.

`GET /v1/admin/review` items carry `flags`, `schema_version`, `prompt_version` and
`source.extraction_method`; `?flag=low_confidence` filters. Each item also carries its
`payload` read with its version's reader (A2 check of 2026-09-29): the value as printed
(`stated_value`, `stated_unit`), the canonical value and unit, the `normalisation` rules, the
counted `floors`, the `land_use_class` and the `table` cell, so the reviewer sees what the
contract did to the printed text.

**A reviewer's correction follows the same rules** (`core/extraction/corrections.py`,
`check_correction`): a number is parsed in the document's conventions (`parse_number`: "2,5",
"1.906,09", "40 %") and normalised to the field's canonical unit (`normalise_number`: ha → m² is
the only conversion); an impossible value is refused (below the field's minimum or at an
exclusive one, a percentage above 100); a value above the field's plausible maximum
(`FIELD_SPECS`) needs `confirm_out_of_range`; floors must parse with the profile's floor tokens
(`parse_floors`); a land use must be a wording the document already uses (its items or served
values, matched without case or accents) or one the profile's land-use terms classify; other texts
are trimmed and bounded (500). A refusal is one 422 problem `{loc, msg, type, ctx}` with the rule
(`not_a_number`, `below_minimum`, `above_maximum`, `out_of_range`, `unit_not_accepted`,
`unknown_floor_notation`, `unknown_land_use`, `not_a_text`, `too_long`); the audit row of an
accepted correction records the rules applied and a confirmed out-of-range value. Notes are
trimmed: an amend note and a rejection reason are required and a note of spaces is no note.

**Stored items stay readable**: `read_payload(schema_version, payload)` uses the reader of the
payload's major version (`PAYLOAD_READERS`); a minor version only adds. A frozen 1.0 item
(`tests/fixtures/extraction/staged-item-v1.0.json`) is read by the tests.

## 9. Evaluation

The corpus evaluation (every parcel of the client's POC documents, scored per field) is
`backend/tests/corpus/README.md`; the checks below are the contract's own.

- **Never guessed** (`cases.py`): five pages where a field is absent and a plausible value is in
  reach: FAR from coverage × floors (and a height), a height from the floor notation in a table,
  a parcel area from gross floor area ÷ FAR, a neighbour's parking rule, an adoption date from the
  plan's date. Unit tests run each with a faithful and a guessing scripted response (the guesses
  come back `unverified`, the neighbour's copy is flagged). `python -m core.extraction eval`
  asks the configured model: a case passes only when the model itself returns `not_found`.
- **Accuracy** on the client's documents: the evaluation corpus (`python -m core.extraction
  corpus ...`, `backend/tests/corpus/README.md`; every parcel row of each POC document). The
  earlier two-page hand-labelled sample was removed on 2026-09-30 (the corpus covers it).
- Commands (from `backend/`): `python -m core.extraction export | eval | corpus ...`.
  `eval` asks the model and costs tokens.

## 10. Open items

- Live model run of the never-guessed cases (needs an Anthropic key on the build machine): the
  acceptance "the model returns null on 5 cases" is checked by `eval`.
- Floor tokens `G` (garaža) and `Pv`, and the land-use term table: to confirm with the client's
  planner. Stara Varoš legend codes (SS, MN, CD, SV, VO, K, SR, TS, U) need a `land_use_legend`
  run on the land-use sheet (its legend text is partly glyph-shifted: decode first).
- Fields without a panel field (other conditions, public-area relation, structured utilities,
  stated gross floor area): kept in the extraction result, not in the review queue.
- Plan-wide averages (e.g. IZ 0.2 for the whole DUP) are notes, not document-level rules, so they
  never become fallback values of parcels.
