# Extraction evaluation corpus

The AI extraction (`core.extraction`, contract in `docs/specs/extraction-contract.md`) is measured
against the client's own planning documents: every urban parcel of their parameter tables,
labelled by hand, and scored field by field. The prompts are iterated against this corpus until
they are review-ready, and CI re-runs it on every prompt or schema change.

## The corpus

| id | document | file (in the client's folder) | pages | parcels | block totals |
|---|---|---|---|---|---|
| `novi-grad-1-2` | Izmjena i dopuna DUP-a "Novi Grad 1 i 2" u Podgorici (DUP) | `02 Novi Grad 1 i 2/Novi Grad 1 i 2 - Urban parcels - Planning parameters.pdf` | 12 | 103 | 6 |
| `stara-varos` | Izmjene i dopune UP "Stara Varoš" (UP) | `03 Stara Varoš/03 Stara Varoš - Urban parcels - Planning parameters.pdf` | 57 | 560 | 7 |

These are the planning-parameter documents of the client's POC set (their shared "Initial GIS
data" folder, mirrored in `docs/gis/source/`). **The PDFs never enter the repository**
(`docs/gis/source/` is git-ignored); `corpus.toml` records each file's path and SHA-256, so a
changed or different file is refused. The other files of the folder (land-use sheets, CAD
drawings) carry no parameter tables and are not in the corpus.

Per document, `<id>/gold.json` holds:

- `parcels`: every urban parcel row of the parameter table with its **12 scored fields** (the 11
  Group 1 fields except the computed ones, plus the block reference): `block_ref`,
  `planned_parcel_area_m2`, `land_use` (text), `max_site_coverage_pct` (IZ), `max_far` (II),
  `max_height_m`, `max_floors` (the floor notation as printed, e.g. `Po+P+4+ M`),
  `building_line_m`, `setback_neighbours_m`, `parking_requirement`, `min_green_area_pct`,
  `utilities`. Each value: `printed` (as on the page), `value` (canonical: IZ ratio → %, numbers
  in the document's conventions), `unit`, `page`, `text`, `cell` (grid cell id). `null` = the
  table states nothing for that parcel: an extracted value there is a **guessed value**. A
  `deferred` value records the wording that defers it ("definisaće se konkursnim rješenjem").
- `blocks`: the block total rows (`UKUPNO BLOK A`, `ukupno D`).
- `document_fields`: the document identity the pages state (name and type from the running
  header; gazette, decision and adoption date are not in these documents, so they must stay
  blank).
- `special_cases` and `not_covered`: what the document exercises (merged cells printed once for a
  group of rows, `***` footnote rows, building sub-rows, a range `Po+P+5-7`, deferred values,
  blank cells) and what the ticket asks for that these documents do not contain (heights in
  metres, a parcel listed twice, "P+2 do P+4" ranges): those are not invented.
- `verification`: who checked which pages against the rendered PDF, when, and the corrections.

## How the gold sets were made

1. `python -m core.extraction corpus label` drafts `gold.json` from the PDF stage's table grids
   with the column map in `corpus.toml` (`[document.labelling]`): which header is which field,
   which rows are parcels, block totals, footnote rows and building sub-rows, how units are
   printed. Merged cells (a value printed once over several rows) are carried to every row they
   span. The draft is deterministic code, not the model under test.
2. Every page was then checked by eye against the rendered page (row by row, digit by digit) and
   the differences corrected in `gold.json`; the `verification` entries record it. A verified gold
   set is never overwritten by `label` without `--force`.

## The metrics

`python -m core.extraction corpus eval` runs the same pipeline the `extract_document` job runs
(PDF stage, chunk plan, one model request per chunk and task, the validator) and scores every
gold cell (parcel × field):

| outcome | meaning |
|---|---|
| `exact` | stated in both, same printed and canonical value |
| `tolerance` | same canonical value printed differently (`1906,09` / `1906.09`, accents or case of a text): a unit-normalised match |
| `wrong` | stated in both, different values |
| `missing` (false blank) | the document states it and the extraction does not (includes values the validator removed as unverified) |
| `false_value` (**hallucinated**) | a value where the document states none, a value where it defers, or any stated value of a parcel the document does not have. **Must be 0.** |
| `correct_blank` | neither states it |

- **accuracy** = (exact + tolerance + correct blank) / cells; the target is ≥ 90 % per document.
- **wrong page**: a correct value cited on another page (citations must be right: 0).
- **confidence calibration**: accuracy of the stated values per confidence bucket
  (0–0.5, 0.5–0.7, 0.7–0.9, 0.9–1.0); the review queue flags values below 0.7.
- parcels found / missed / extra, block totals found, document identity fields.
- **tokens and cost** per document (input incl. cache reads / writes, output; USD at list price).

The full report of a run (every error with the gold and the extracted value) is written to
`backend/.cache/extraction-eval/runs/` (git-ignored: it quotes the documents). Each run appends
one line to `results/log.jsonl`, and `RESULTS.md` is rebuilt from it: that is the iteration log
(prompt version, model, effort, scores, note).

## Commands (from `backend/`)

```bash
python -m core.extraction corpus eval --live                  # the whole corpus, call the model on cache misses
python -m core.extraction corpus eval --doc stara-varos --pages 1-8 --live   # a sample while iterating
python -m core.extraction corpus eval                         # replay from the reply cache only (free)
python -m core.extraction corpus baseline                     # accept the last full run as the regression baseline
python -m core.extraction corpus check                        # the last run against baseline.json
python -m core.extraction corpus report                       # rebuild RESULTS.md
```

`ANTHROPIC_API_KEY` in `backend/.env`; `ANTHROPIC_BASE_URL` pinned to `https://api.anthropic.com`.
Every model reply is cached under the hash of its request (model, system blocks, user message,
schema) in `backend/.cache/extraction-eval/replies/`, so an unchanged request is never paid twice
and a prompt change re-reads only what it changes. `CORPUS_SOURCE_DIR` points at the documents
(default `../docs/gis/source`).

## Regression check (CI)

`.github/workflows/extraction-eval.yml` runs on any change to `core/extraction/` (prompts, schema,
validator, chunking, pre-processing), the municipality profiles or this corpus: it fetches the PDFs
from a private link (`CORPUS_ARCHIVE_URL` secret), restores the reply cache, runs
`corpus eval --live --check` and fails when any value is hallucinated, a wrong page citation
appears, or a document's accuracy falls more than half a point below `baseline.json`. After an
accepted improvement, run the full corpus and `corpus baseline`, and commit `baseline.json`.

## Adding a document (when the client uploads more)

1. Put the PDF in the client folder mirror (`docs/gis/source/...`) and add a `[[document]]` to
   `corpus.toml`: `id`, `name`, `type` (a key of the profile's `document_types`), `parameters`
   (path), `sha256` (`sha256sum`), `pages`, and `[document.labelling]`: the header of the parcel
   number column (and the block column), one `columns` entry per field with its header text
   (accent-folded, `exact` or `prefix`) and how the unit is printed (`ratio` for an IZ printed as
   0.4, `percent`, `m2`, `words`), and regexes for block total rows, footnote rows, building
   sub-rows and deferring wording as the document has them. List `special_cases` and
   `not_covered`.
2. `python -m core.extraction corpus label --doc <id>` drafts `<id>/gold.json`.
3. Check every page against the rendered PDF, correct `gold.json`, add a `verification` entry.
   Values the document states outside the table (a document-wide parking rule, a building line in
   the text) are added by hand with their page.
4. `python -m core.extraction corpus eval --doc <id> --live`, read the error examples, iterate the
   prompts (a new `prompt_sets/vX.Y/`, `PROMPT_VERSION`) until the targets hold, then run the whole
   corpus and set the baseline.
