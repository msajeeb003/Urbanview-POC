# The `extract_document` job: extraction runs

Status: built 2026-09-26 (migration 0020). Code: `backend/jobs/extraction_runner.py` (the body),
`backend/jobs/tasks/extraction.py` (the Celery task), `backend/core/extraction/runs.py` (shared
statements), `backend/core/extraction/staging.py` (items). Contract of what is extracted:
[extraction-contract.md](extraction-contract.md); pages and chunks:
[pdf-preprocessing.md](pdf-preprocessing.md).

Every registered planning document version is read in the background, end to end, into items a
reviewer approves. Nothing the job writes reaches the public map: the review queue decides and the
publish job serves.

## Trigger and idempotency

`POST /v1/admin/documents/{id}/jobs/extract[?force=true]` (role admin) queues one
`extract_document` job on the `extraction` queue and, in the same transaction, one
`extraction_runs` row (`queued`).

A run is identified by **the document version, its file's SHA-256, the configured model
(`EXTRACTION_MODEL`), `PROMPT_VERSION` and `SCHEMA_VERSION`**:

| Situation | Answer |
|---|---|
| A run with that key already reached `ready_for_review` | **200** with that run's job (its summary); nothing is re-read. `?force=true` reads again. |
| An identical job is queued / running / retrying | **200** with that job (dedupe key `extract_document:document:{id}:sha256:{sha}:model:{m}:prompt:{p}:schema:{s}`) |
| Anything else (first read, a newer prompt / schema / model, another file) | **202** with a new job and run |

Uploading the identical file again is a no-op at the file level (`stored_files` dedupe by
checksum) and at the run level (the finished run answers).

## Status flow

`queued` (enqueued) → `extracting` (the worker started; retried jobs resume) →
`ready_for_review` (items written) | `failed` (see below). The document and file records carry the
latest run (`DocumentOut.extraction`, `StoredFileOut.extraction`: status, pages failed / skipped,
items, tokens, cost); every job carries its run (`JobOut.extraction_run`).

## The pipeline

1. **Pages.** The file's pre-processing manifest when it is current for the checksum
   (`stored_files.preprocess`), else the PDF stage runs first (`PreprocessRunner`, cached by
   checksum). Scanned pages nobody could read (no OCR) are skipped and listed
   (`pages_skipped`); a file with nothing readable fails.
2. **Chunk selection.** Every chunk of the plan that suggests extraction tasks (planning
   sections, tables, the first pages for the document task), once per task. Chunks without
   planning content are counted (`chunks_without_planning_content`), not read. The land-use
   legend is read first so later steps classify legend codes. More than `EXTRACTION_MAX_CHUNKS`
   steps fail the run instead of spending.
3. **One structured request per step** (`core.extraction.run.run_task`): prompt set
   `PROMPT_VERSION`, the response schema of the task, validation into the canonical contract
   (`SCHEMA_VERSION`). The model transcribes; code types, normalises and checks.
4. **Targets.** Parcel numbers and block labels are matched (normalised: `parcel_key`,
   `block_key`) to the document's urban parcels and to the blocks its parcels or coverage touch.
   A value whose target matches no geometry is **still staged**, as a text reference:
   `urban_parcel_id` / `block_id` null, the number as printed in `target_label`, flag
   `target_unmatched` (or `target_staged` when it matches geometry the GIS job staged but nobody
   has published yet). The publish job never serves such an item (no parcel to attach it to).
5. **Items** (`planning_parameter_extractions`, one transaction at the end, so the queue never
   shows half a run): one row per stated value with parameter, target, value, unit, raw text,
   page, bbox, confidence, extraction method, flags, `schema_version`, `prompt_version`,
   `extracted_by = llm:<model version>`, `run_id`, `job_id`, `review_state = pending_review`. The
   same target and field read twice in one run is flagged `repeated_in_run`.

## Failures

| What happens | Handling |
|---|---|
| Transient model error (overload, timeout, 5xx, 429) | retried in the job with exponential backoff (`EXTRACTION_CALL_RETRIES`, `EXTRACTION_RETRY_BASE_SECONDS`, `..._MAX_SECONDS`); then the job retries (`TransientError` / `RateLimited`, the base task's backoff) and **resumes**: every finished step is checkpointed in `extraction_run_chunks` and never paid for twice |
| An answer that does not fit the schema | asked once more with the validator's error appended to the request; a second failure fails **that step only** |
| Refusal, bad request, authentication | that step fails (not retried) |
| Some steps failed | the run ends `ready_for_review`; `pages_failed` lists `{page, chunk, task, error}` ("n pages failed") |
| Every step failed, nothing readable, file changed after queueing, no file | the run and the job end `failed` with the reason |

A manual retry of a failed job (`POST /v1/admin/jobs/{id}/retry`) reads the failed steps again and
keeps the finished ones.

## Re-extraction and superseding (never deleting)

Each new item links to the previous reading of the same target and field in the document's
lineage (`previous_item_id`) and says `change`: `new` | `same` | `changed` (value and unit, the
amended value when the previous item was amended), so the reviewer sees what changed.

When a run finishes, what it replaces is **superseded** (`superseded_at`,
`superseded_by_run_id`), never deleted:

| Earlier items | Superseded |
|---|---|
| Same document version, same file, older prompt / schema / model | the **pending** ones; approved, amended and rejected items stay |
| Same document version, another file | every open (unpublished) item |
| Older versions of the document (lineage) | every open item |
| Published items, manual or seeded items (no run) | never |

The earlier run records `superseded_by_run_id`. Approving or amending a newer reading of a target
retires the older approved item it replaces (so the older value can never publish after the newer
one). Superseded items leave the queue (`?include_superseded=true` shows them), the counters,
`can_publish` and the publish job; a decision on one answers 409 `superseded`.

## Tracking and audit

- `extraction_runs`: model and model version, prompt / schema / pre-processing versions, pages
  processed / skipped / failed, chunks done / failed, items written / low_confidence / unmatched,
  items superseded, tokens in / out, estimated cost, the summary.
- The job: progress per step while running, the run summary as `result`, the model and tokens in
  its cost block (`GET /v1/admin/jobs/{id}`, `GET /v1/admin/jobs/costs`).
- `audit_log`: `extraction.start` (every attempt) and `extraction.finish` (status, item counts,
  pages failed / skipped, tokens) on entity `extraction_run`, actor `worker:extract_document`.

## Review queue additions

`GET /v1/admin/review` items carry `run_id`, `change`, `previous` (id, status, effective value,
run), `superseded*`, and `target.label` / `target.matched`; filters `run_id`, `change`,
`include_superseded`, and flags such as `target_unmatched`, `repeated_in_run`.

## Checked

- `tests/test_extraction_job_unit.py`: step order, change detection, the key, unmatched staging,
  the validation retry with the error fed back, transient backoff, refusals.
- `tests/integration/test_extraction_job_postgis.py` (a scripted model that copies the parameter
  table, `tests/extraction_script.py`): items for every parcel, pending; idempotency; one bad page
  listed while the rest is ready; a transient outage retried by the job that resumes from the
  checkpoints; re-extraction keeping decisions and linking changes; a new file version superseding
  the old version's items; audit rows; the run summary on the job.
- The same path (without the database) over the client's POC tables with that scripted model:
  Novi Grad 103 of 103 parcels (462 values), Stara Varoš 567 of 567 (2,821 values).

## Open

- **No live model run yet**: needs `ANTHROPIC_API_KEY` in the worker; costs tokens.
- A value staged as `target_unmatched` gets its parcel id only by a new run after the geometry is
  published (`?force=true`); there is no automatic re-link yet.
