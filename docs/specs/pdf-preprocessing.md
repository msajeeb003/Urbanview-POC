# PDF pre-processing for AI extraction (version 1.0)

Status: engineering contract for the POC (2026-09-26). Code: `backend/core/extraction/preprocess.py`,
`chunking.py`, `manifest.py`, `backend/jobs/preprocessing.py`, job `preprocess_file`. It turns a
stored planning PDF into what the extraction prompts read (`docs/specs/extraction-contract.md`),
page by page, keeping every position so each extracted value cites a page and a box.

## 1. Pages (`extract_pages(source)`, pymupdf)

Per page: `number`, `width` / `height` / `rotation` (PDF user space), `text` (the blocks in
reading order, top to bottom then left to right), `blocks[]` `{id, text, bbox, words range,
font_size, bold, table, decoded, section}`, `words[]` `{text, bbox}`, `tables[]`, `char_count`,
`image_coverage`, `path_count`, `scanned` + `scanned_reason`, `blank`, `method` (text | ocr |
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
  (2) characters per 10 000 pt² (`low_text_density`). A scanned page is read only by a configured
  OCR backend (`EXTRACTION_OCR_BACKEND=tesseract`, languages `srp_latn+srp`, method `ocr`);
  otherwise it stays unread (method `none`) and is listed for manual handling. Text is never made
  up. OCR is outside the POC scope: the default is `none`.

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

## 3. Manifest, cache, page images (`manifest.py`, `jobs/preprocessing.py`)

`POST /v1/admin/files/{id}/jobs/preprocess[?force=true]` (admin; planning-document PDFs only,
409 otherwise; 202 / 200 idempotent) queues `preprocess_file` (queue `extraction`). The job:

1. skips the analysis when `stored_files.preprocess` is current (same SHA-256,
   `PREPROCESS_VERSION` and options key); otherwise reads the PDF (checksum verified), analyses
   it, stores the page data as gzip JSON next to the upload
   (`{m}/uploads/planning_document/{sha256}/preprocess-v1.0.json.gz`) and the manifest (migration
   0018) on the file record: pages, tables, chunk plan in reading priority, page image keys per
   document, and `summary` (page count, vector pages, scanned / OCR / unread / blank pages,
   tables, chunks, sections with their pages, scripts);
2. renders page images (PNG at `PREPROCESS_PAGE_IMAGE_DPI` 150, large sheets capped at
   `PREPROCESS_PAGE_IMAGE_MAX_PIXELS` 25 Mpx) for every document version registered on the file
   that lacks them, at the source viewer's keys. They are served (`page_images_rendered`) only
   with `PREPROCESS_SERVE_PAGE_IMAGES=true`: the public source viewer highlights a cited value on
   the PDF, not on an image.

`GET /v1/admin/files/{id}` and `GET /v1/admin/documents/{id}` carry `preprocessing` (the summary),
so a document lists its scanned pages. The extraction job starts from the manifest
(`jobs.preprocessing.load_document_pages` + `chunk_pages`).

## 4. Measured on the POC documents

DUP Novi grad 1 i 2 parameters (12 pages, 18 tables, 6 continuations stitched) in about 5 s;
UP Stara Varoš parameters (57 pages, one table per page with its own header) in about 50 s (pymupdf's
table finder dominates); both all vector, Latin, headers exact. The seven sheets of both documents
give text with boxes (table finding skipped: drawing sheets).
