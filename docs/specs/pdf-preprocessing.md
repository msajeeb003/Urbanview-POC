# PDF pre-processing for AI extraction (version 1.0)

Status: engineering contract for the POC (2026-09-26). Code: `backend/core/extraction/preprocess.py`,
`chunking.py`, `manifest.py`, `backend/jobs/preprocessing.py` (`run_preprocess`, the first stage of the
extraction and geometry jobs). It turns a
stored planning PDF into what the extraction prompts read (`docs/specs/extraction-contract.md`),
page by page, keeping every position so each extracted value cites a page and a box.

## 1. Pages (`extract_pages(source)`, pymupdf)

Per page: `number`, `width` / `height` / `rotation` (PDF user space), `text` (the blocks in
reading order, top to bottom then left to right), `blocks[]` `{id, text, bbox, words range,
font_size, bold, table, decoded, section}`, `words[]` `{text, bbox}`, `tables[]`, `char_count`,
`image_coverage`, `largest_image_pct`, `path_count`, `raster`, `scanned` + `scanned_reason`,
`blank`, `method` (text |
none), `script` (latin | cyrillic | mixed | none), `headings[]`, `sections[]`.

- **Boxes** are PDF points with the origin bottom-left, rotation undone (the review queue's and
  the source viewer's space).
- **Text** is kept as extracted, NFC-composed: č ć š ž đ and Cyrillic come out unchanged. On pages
  that show the AutoCAD glyph-id shift ("SRYUåLQH"), the shifted words are decoded with
  `core.gis` ("površine") and the block is marked `decoded`.
- **Tables**: pymupdf's table finder on ruled lines (`lines_strict`, then `lines`), a
  text-alignment layout heuristic last (kept only when table-shaped). Cell text is rebuilt from
  the page's lines in reading order (rotated header text included); a word or number wrapped
  inside a narrow cell is joined ("Površin" + "a UP" -> "Površina UP", "1906.0" + "9" ->
  "1906.09"; only a 1–3 letter tail or more digits after a single token that runs to the cell
  edge). Leading rows without numbers are the header (`header_rows`, `columns`). Every cell has an
  id `rNcM` and a box. Skipped on drawing sheets (> `PREPROCESS_TABLE_MAX_PATHS` vector paths or
  larger than about A2).
- **Stitching**: a header-less table with the columns of the table before it (same count, column
  edges within 3 pt) continues it (`continues`) and takes its column names (`header_from`).
- **Scanned**: images cover ≥ `PREPROCESS_SCANNED_IMAGE_COVERAGE` (0.5) of the page, at most 200
  vector paths, and no text layer (`no_text_layer`) or fewer than `PREPROCESS_MIN_TEXT_DENSITY`
  (2) characters per 10 000 pt² (`low_text_density`). A scanned page is flagged and never read:
  it stays unread (method `none`), gets no chunk and is listed (`unread_pages`) for manual handling
  (its geometry is redrawn in QGIS). Text is never made up; there is no OCR.
- **Raster sheet** (version 1.2, the A1 check of 2026-09-29): the week-1 geometry assessment's
  own rule, shared from `core.gis.sheets` (`is_raster_sheet`): the largest single image covers ≥
  60 % of the page (`largest_image_pct`) and fewer than 1000 vector paths are drawn on it. Such a
  page is the assessment's class C: nothing to extract, georeference and redraw it in QGIS. The
  summary lists them as `redraw_pages` (null in a manifest of an older version: the admin API then
  names its scanned pages); the Data sources list flags the file "needs QGIS redraw".

## 2. Sections and chunks (`chunking.py`)

- **Sections** (methodology step 2a–d): numbered ("4.", "4.2", "IV.", "a)"), larger, bold or
  upper-case short blocks are headings, running headers / footers excepted. A heading that
  matches `[extraction.sections]` of the municipality profile (`urban_parcels`, `regulation`,
  `land_use`, `infrastructure`; accent-folded, Cyrillic transliterated) sets the section until a
  numbered heading of the same or a higher level that matches nothing. A table's column names add
  the sections they match ("Broj UP" -> urban parcels, "Indeks zauzetosti" -> regulation).
- **Chunks**: one per page by default; elements (text blocks outside tables, whole tables) in
  reading order within `PREPROCESS_CHUNK_TOKEN_BUDGET` (6000, estimated at
  `PREPROCESS_CHARS_PER_TOKEN` 3); a table is never split (alone over budget: `over_budget`).
  A continued table is shown with the columns of the table it continues. Chunks with a planning
  section or table come first (`priority` 0) and suggest their extraction tasks
  (`parameter_table`, `urban_parcel`, `block`, `land_use_legend`, `infrastructure`, `document`).
- `chunk_pages(doc, chunk)` gives the contract's `PageInput`s: `text` + `words` (what citations are
  checked against, with boxes), the table grids (cell ids and boxes) and the `view` the model
  reads (`=== Page N ===` markers, `[Table p2t1: continues p1t1; columns from p1t1 on page 1: …]`,
  one line per row `r4 | c0: A | c1: UP 12 | …`).

## 3. Manifest and cache (`manifest.py`, `jobs/preprocessing.py`)

The extraction job runs the stage first when the file's manifest is missing or stale, and the
geometry job runs it for a PDF drawing (`jobs.tasks.extraction.run_preprocess`; planning-document
PDFs only; there is no separate trigger since 2026-09-30). The stage skips the analysis when
`stored_files.preprocess` is current (same SHA-256, `PREPROCESS_VERSION` and options key);
otherwise it reads the PDF (checksum verified), analyses it, stores the page data as gzip JSON
next to the upload (`{m}/uploads/planning_document/{sha256}/preprocess-v{PREPROCESS_VERSION}.json.gz`)
and the manifest (migration 0018) on the file record: pages, tables, chunk plan in reading
priority, and `summary` (page count, vector pages, scanned / unread / blank / redraw pages,
tables, chunks, sections with their pages, scripts). No page images are rendered: the source
viewer and the review queue show the PDF page itself (`#page=N`) with the value's box.

The upload reply (`POST /v1/admin/files`) and `GET /v1/admin/documents/{id}` carry
`preprocessing` (the summary), so a document lists its scanned pages. The extraction job starts from the manifest
(`jobs.preprocessing.load_document_pages` + `chunk_pages`).

## 4. Measured on the POC documents

DUP Novi grad 1 i 2 parameters (12 pages, 18 tables, 6 continuations stitched) in about 5 s;
UP Stara Varoš parameters (57 pages, one table per page with its own header) in about 50 s (pymupdf's
table finder dominates); both all vector, Latin, headers exact. The seven sheets of both documents
give text with boxes (table finding skipped: drawing sheets).
