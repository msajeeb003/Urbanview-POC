"use client";

/**
 * The "Data sources" tab (`/admin/data`, wireframe `adminData`): the public sources card with
 * "+ Upload document", then the planning documents with their files.
 *
 * Documents table: one row per current version (name → its page, zone, type, status, version,
 * files, overall state, coverage, actions) and under it one row per file with its extraction and
 * geometry job (queued / running / succeeded / failed, attempts and cost when known). Actions:
 * queue extraction (the text / both PDFs), queue geometry (the drawing / both files), retry a
 * failed job, rerun an extraction, mark the coverage live or not, register a new version. The page
 * re-reads its data every 3 s while a job is queued or running (`AutoRefresh`), and stops after.
 * Filters (zone, status, state, job state, name) live in the URL. Reviewers read it without the
 * write controls (`readOnly`: the pilot scope's "read-only documents").
 */
import Form from "next/form";
import Link from "next/link";
import { useState } from "react";

import {
  anyActive,
  DATA_SOURCES,
  documentHref,
  DOCUMENT_STATES,
  DOCUMENT_STATUSES,
  emptyDocumentsText,
  extractionPill,
  filtersHref,
  hasFilters,
  JOB_STATE_FILTERS,
  jobPill,
  PAGE_SIZE,
  reviewHref,
  roleLabel,
  stateChip,
  statusChip,
  type DocumentFilters,
} from "@/lib/admin/data";
import {
  queueExtractionAction,
  queueGeometryAction,
  retryJobAction,
  setCoverageLiveAction,
} from "@/lib/admin/data-actions";
import type { AdminDocument, AdminDocumentFile } from "@/lib/api/types";

import { AdminButton, AdminCard, DataTable, StatusChip } from "../parts";

import { ActionButton, AutoRefresh, DataReadOnly, PillView, useDataReadOnly } from "./parts";
import { RegisterDialog, type PickedFile, type TypeOption, type ZoneOption } from "./register-dialog";
import { UploadDialog } from "./upload-dialog";

const LINKED = <StatusChip tone="ok">Linked</StatusChip>;

function textFiles(doc: AdminDocument): AdminDocumentFile[] {
  return (doc.files ?? []).filter((f) => f.kind === "planning_document" && f.role !== "drawing");
}

function drawingFiles(doc: AdminDocument): AdminDocumentFile[] {
  return (doc.files ?? []).filter((f) => f.role !== "text");
}

function coverageText(doc: AdminDocument): string {
  if (!doc.has_coverage) return "No coverage yet";
  return doc.coverage_live ? "Coverage live" : "Coverage ready, not live";
}

export function DocumentActions({ doc, onNewVersion }: { doc: AdminDocument; onNewVersion?: () => void }) {
  const readOnly = useDataReadOnly();
  const text = textFiles(doc);
  const drawings = drawingFiles(doc);
  return (
    <div className="rowacts">
      {text.length > 0 && (
        <ActionButton
          title="Queue the AI extraction of every text / both PDF (files already read the same way are skipped)"
          action={() => queueExtractionAction(doc.id, text.map((f) => f.file_id))}
        >
          Queue extraction
        </ActionButton>
      )}
      {drawings.length > 0 && (
        <ActionButton
          title="Queue the geometry job for every drawing / both file"
          action={() => queueGeometryAction(drawings.map((f) => f.file_id))}
        >
          Queue geometry
        </ActionButton>
      )}
      {doc.is_current_version && (
        <ActionButton
          disabled={!doc.has_coverage && !doc.coverage_live}
          title={doc.has_coverage ? undefined : "No coverage area yet: run the geometry job first"}
          action={() => setCoverageLiveAction(doc.id, !doc.coverage_live)}
        >
          {doc.coverage_live ? "Mark not live" : "Mark live"}
        </ActionButton>
      )}
      {onNewVersion && doc.is_current_version && !readOnly && (
        <button type="button" className="abtn sm ghost" onClick={onNewVersion}>
          New version…
        </button>
      )}
    </div>
  );
}

export function FileActions({ doc, file }: { doc: AdminDocument; file: AdminDocumentFile }) {
  const state = file.extraction_state ?? "none";
  const job = file.extraction_job;
  const geo = file.geometry_job;
  const pending = file.items?.pending ?? 0;
  return (
    <div className="rowacts">
      {state === "failed" && job && (
        <ActionButton title="Run the failed extraction again (finished steps are not paid twice)" action={() => retryJobAction(job.id)}>
          Retry
        </ActionButton>
      )}
      {state === "none" && file.role !== "drawing" && file.kind === "planning_document" && (
        <ActionButton action={() => queueExtractionAction(doc.id, [file.file_id])}>Extract</ActionButton>
      )}
      {state === "ready_for_review" && (
        <ActionButton
          title="Read the file again with the AI; the new items replace the pending ones"
          confirm={`Read ${file.original_filename} again with the AI? This costs tokens; pending items are replaced, decisions stay.`}
          action={() => queueExtractionAction(doc.id, [file.file_id], true)}
        >
          Rerun
        </ActionButton>
      )}
      {geo && (geo.status === "failed" || geo.status === "cancelled") && (
        <ActionButton title="Run the geometry job again" action={() => retryJobAction(geo.id)}>
          Retry geometry
        </ActionButton>
      )}
      {state === "ready_for_review" && pending > 0 && (
        <Link className="abtn sm" href={reviewHref(doc.id, file.file_id)}>
          Review {pending} →
        </Link>
      )}
    </div>
  );
}

function DocumentsTable({ documents, onNewVersion }: { documents: AdminDocument[]; onNewVersion: (doc: AdminDocument) => void }) {
  return (
    <table className="tbl doctbl">
      <thead>
        <tr>
          <th>Document</th>
          <th>Zone</th>
          <th>Type</th>
          <th>Status</th>
          <th>Version</th>
          <th>Files</th>
          <th>Extraction</th>
          <th>Geometry</th>
          <th aria-label="Actions" />
        </tr>
      </thead>
      {documents.map((doc) => {
        const status = statusChip(doc.status);
        const state = stateChip(doc.state ?? "no_files");
        const review = doc.review;
        return (
          <tbody key={doc.id} className="docgroup">
            <tr className="docrow">
              <td>
                <Link className="docname" href={documentHref(doc.id)}>
                  {doc.name}
                </Link>
              </td>
              <td>{doc.zone_name ?? "—"}</td>
              <td className="mono">{doc.type}</td>
              <td>
                <StatusChip tone={status.tone}>{status.label}</StatusChip>
              </td>
              <td className="mono">v{doc.version}</td>
              <td className="mono">{doc.files?.length ?? 0}</td>
              <td>
                <span className="pillcell">
                  <StatusChip tone={state.tone}>{state.label}</StatusChip>
                  {review && review.total > 0 && (
                    <span className="mono pd">
                      {review.pending} pending · {review.approved + review.amended} accepted
                    </span>
                  )}
                </span>
              </td>
              <td className="covtext">{coverageText(doc)}</td>
              <td>
                <DocumentActions doc={doc} onNewVersion={() => onNewVersion(doc)} />
              </td>
            </tr>
            {(doc.files ?? []).map((file) => (
              <tr key={file.file_id} className="filerow">
                <td colSpan={5}>
                  <span className="fname" title={file.original_filename}>
                    ↳ {file.original_filename}
                  </span>
                  <span className="fmeta mono">
                    {roleLabel(file.role)}
                    {file.page_count ? ` · ${file.page_count} p.` : ""}
                    {file.scanned_pages?.length ? ` · ${file.scanned_pages.length} scanned` : ""}
                  </span>
                </td>
                <td />
                <td>
                  <PillView pill={extractionPill(file)} />
                </td>
                <td>
                  <PillView pill={jobPill(file.geometry_job, file.role)} />
                </td>
                <td>
                  <FileActions doc={doc} file={file} />
                </td>
              </tr>
            ))}
          </tbody>
        );
      })}
    </table>
  );
}

export function DataScreen({
  documents,
  total,
  filters,
  zones,
  types,
  readOnly = false,
}: {
  documents: AdminDocument[];
  total: number;
  filters: DocumentFilters;
  zones: ZoneOption[];
  types: TypeOption[];
  /** Reviewers: documents, files and jobs without the write controls. */
  readOnly?: boolean;
}) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const [register, setRegister] = useState<{ key: number; files: PickedFile[]; replaces: AdminDocument | null } | null>(
    null,
  );
  const openRegister = (files: PickedFile[], replaces: AdminDocument | null = null) =>
    setRegister({ key: Date.now(), files, replaces });
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = hasFilters(filters);

  return (
    <DataReadOnly.Provider value={readOnly}>
      <AutoRefresh active={anyActive(documents)} />
      <AdminCard
        title="Data sources"
        sub="Public official data — version-controlled, licence recorded"
        action={readOnly ? undefined : <AdminButton onClick={() => setUploadOpen(true)}>+ Upload document</AdminButton>}
      >
        <DataTable
          rows={[...DATA_SOURCES]}
          rowKey={(s) => s.source}
          columns={[
            { key: "source", label: "Source", render: (s) => <b>{s.source}</b> },
            { key: "provides", label: "Provides", render: (s) => s.provides },
            { key: "format", label: "Format", mono: true, render: (s) => s.format },
            { key: "status", label: "Status", render: () => LINKED },
          ]}
        />
      </AdminCard>

      <AdminCard
        title="Planning documents"
        sub={`${total} ${filtered ? "matching " : ""}document${total === 1 ? "" : "s"} · current versions · each file's extraction and geometry`}
        action={readOnly ? undefined : <AdminButton onClick={() => openRegister([])}>+ New document</AdminButton>}
      >
        <Form action="/admin/data" className="datafilters" role="search">
          <input type="search" name="q" defaultValue={filters.q} placeholder="Search by name" aria-label="Search by name" />
          <select
            name="zone"
            defaultValue={filters.zone ?? ""}
            aria-label="Zone"
            onChange={(e) => e.currentTarget.form?.requestSubmit()}
          >
            <option value="">All zones</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id}>
                {z.name}
              </option>
            ))}
          </select>
          <select
            name="status"
            defaultValue={filters.status ?? ""}
            aria-label="Status"
            onChange={(e) => e.currentTarget.form?.requestSubmit()}
          >
            <option value="">Any status</option>
            {DOCUMENT_STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            name="state"
            defaultValue={filters.state ?? ""}
            aria-label="State"
            onChange={(e) => e.currentTarget.form?.requestSubmit()}
          >
            <option value="">Any state</option>
            {DOCUMENT_STATES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            name="job"
            defaultValue={filters.job ?? ""}
            aria-label="Job state"
            onChange={(e) => e.currentTarget.form?.requestSubmit()}
          >
            <option value="">Any job state</option>
            {JOB_STATE_FILTERS.map((s) => (
              <option key={s.value} value={s.value}>
                Job {s.label.toLowerCase()}
              </option>
            ))}
          </select>
          <button type="submit" className="abtn sm">
            Filter
          </button>
          {filtered && (
            <Link className="abtn sm ghost" href="/admin/data">
              Clear
            </Link>
          )}
        </Form>
        {documents.length === 0 ? (
          <div className="admin-note">{emptyDocumentsText(filters)}</div>
        ) : (
          <DocumentsTable documents={documents} onNewVersion={(doc) => openRegister([], doc)} />
        )}
        {pages > 1 && (
          <div className="admin-note pager">
            Page {filters.page} of {pages}
            {filters.page > 1 && (
              <Link className="abtn sm ghost" href={filtersHref(filters, filters.page - 1)}>
                ‹ Previous
              </Link>
            )}
            {filters.page < pages && (
              <Link className="abtn sm ghost" href={filtersHref(filters, filters.page + 1)}>
                Next ›
              </Link>
            )}
          </div>
        )}
      </AdminCard>

      <UploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onRegister={(files) => {
          setUploadOpen(false);
          openRegister(files);
        }}
      />
      {register && (
        <RegisterDialog
          key={register.key}
          open
          onOpenChange={(open) => !open && setRegister(null)}
          zones={zones}
          types={types}
          initialFiles={register.files}
          replaces={register.replaces}
        />
      )}
      {readOnly && <div className="finfoot">Read only: administrators register documents, upload files and run the jobs.</div>}
    </DataReadOnly.Provider>
  );
}
