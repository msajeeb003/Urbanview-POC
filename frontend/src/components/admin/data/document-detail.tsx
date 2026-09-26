"use client";

/**
 * One planning document version (`/admin/data/documents/[id]`): its registry facts, its files and
 * its version history.
 *
 * Files: drop PDFs (several at once) after choosing what they are read for ("Add as" text /
 * drawing / both). Each file uploads with a progress bar, joins the version and, unless it is a
 * drawing, goes straight to the AI extraction; its row then moves queued → extracting → ready for
 * review (or failed, with the reason) while the page re-reads its data every 3 s, and polling
 * stops once every file is finished. The same PDF dropped again is recognised by its checksum:
 * "already uploaded", the existing row, no new one. Per file: pages, scanned pages, extraction
 * state and cost, the geometry job, rerun / retry, the review queue filtered to the file, and
 * "Remove" (refused once an item read from it was approved).
 */
import Link from "next/link";
import { useState, useTransition } from "react";

import {
  ACCEPT_PDF,
  documentActive,
  documentHref,
  extractionPill,
  FILE_ROLES,
  formatCost,
  jobPill,
  removeBlockerText,
  reviewHref,
  rolesFor,
  stateChip,
  statusChip,
} from "@/lib/admin/data";
import {
  attachFilesAction,
  queueExtractionAction,
  removeFileAction,
  setFileRoleAction,
} from "@/lib/admin/data-actions";
import { relativeTime, utcStamp } from "@/lib/admin/format";
import type { AdminDocument, AdminDocumentFile, FileRole } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, StatusChip } from "../parts";

import { DocumentActions, FileActions } from "./data-screen";
import { ActionButton, AutoRefresh, PillView } from "./parts";
import { RegisterDialog, type TypeOption, type ZoneOption } from "./register-dialog";
import { DropZone, UploadList, useUploads } from "./uploads";

function RoleSelect({ doc, file }: { doc: AdminDocument; file: AdminDocumentFile }) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  return (
    <select
      className="rolesel"
      aria-label={`Role of ${file.original_filename}`}
      value={file.role}
      disabled={pending || !doc.is_current_version || file.kind === "gis"}
      onChange={(e) => {
        const role = e.target.value as FileRole;
        start(async () => {
          const result = await setFileRoleAction(doc.id, file.file_id, role);
          showToast(result.message);
        });
      }}
    >
      {FILE_ROLES.filter((r) => rolesFor(file.kind).includes(r.value)).map((r) => (
        <option key={r.value} value={r.value} title={r.hint}>
          {r.label}
        </option>
      ))}
    </select>
  );
}

function FilesTable({ doc }: { doc: AdminDocument }) {
  const files = doc.files ?? [];
  if (!files.length) {
    return (
      <div className="admin-note">
        No files yet. Drop the document&apos;s PDFs above: text parts are read by the AI extraction, drawing sheets by the
        geometry job.
      </div>
    );
  }
  return (
    <table className="tbl filetbl">
      <thead>
        <tr>
          <th>File</th>
          <th>Role</th>
          <th>Pages</th>
          <th>Scanned</th>
          <th>Extraction</th>
          <th>Cost</th>
          <th>Geometry</th>
          <th aria-label="Actions" />
        </tr>
      </thead>
      <tbody>
        {files.map((file) => {
          const blocker = removeBlockerText(file.remove_blocker);
          const cost = formatCost(file.extraction?.estimated_cost_eur ?? file.extraction_job?.cost?.estimated_cost_eur);
          return (
            <tr key={file.file_id} id={`file-${file.file_id}`}>
              <td>
                <span className="fname" title={file.original_filename}>
                  {file.original_filename}
                </span>
                <span className="fmeta mono">
                  #{file.file_id}
                  {file.is_primary ? " · primary" : ""} · added {relativeTime(file.added_at)}
                </span>
              </td>
              <td>
                <RoleSelect doc={doc} file={file} />
              </td>
              <td className="mono">{file.page_count ?? "—"}</td>
              <td className="mono" title={file.scanned_pages?.length ? `Pages ${file.scanned_pages.join(", ")}` : undefined}>
                {file.scanned_pages == null ? "—" : file.scanned_pages.length}
              </td>
              <td>
                <PillView pill={extractionPill(file)} />
              </td>
              <td className="mono">{cost ?? "—"}</td>
              <td>
                <PillView pill={jobPill(file.geometry_job, file.role)} />
              </td>
              <td>
                <div className="rowacts">
                  <FileActions doc={doc} file={file} />
                  {doc.is_current_version && (
                    <ActionButton
                      disabled={!file.can_remove}
                      title={blocker ?? "Take the file off this version (the stored file is kept)"}
                      confirm={`Remove ${file.original_filename} from this document? Its pending review items leave the queue.`}
                      action={() => removeFileAction(doc.id, file.file_id)}
                    >
                      Remove
                    </ActionButton>
                  )}
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function DocumentDetail({ doc, zones, types }: { doc: AdminDocument; zones: ZoneOption[]; types: TypeOption[] }) {
  const [addAs, setAddAs] = useState<FileRole>("text");
  const [versionKey, setVersionKey] = useState<number | null>(null);
  const uploads = useUploads({
    pdfOnly: true,
    defaultRole: addAs,
    onUploaded: async (item) => {
      const stored = item.stored!;
      if ((doc.files ?? []).some((f) => f.file_id === stored.id)) {
        return "already on this document — its row is below";
      }
      const attached = await attachFilesAction(doc.id, [{ fileId: stored.id, role: item.role }]);
      if (!attached.ok) return attached.message;
      if (!attached.data.added) return "already on this document — its row is below";
      if (item.role === "drawing") return "added to the document (read by the geometry job)";
      const queued = await queueExtractionAction(doc.id, [stored.id]);
      return queued.ok ? "added · extraction queued" : `added · ${queued.message}`;
    },
  });
  const status = statusChip(doc.status);
  const state = stateChip(doc.state ?? "no_files");
  const review = doc.review;
  const current = doc.versions?.find((v) => v.is_current_version);

  return (
    <>
      <AutoRefresh active={documentActive(doc) || uploads.busy} />
      <div className="crumbs">
        <Link href="/admin/data">← Data sources</Link>
      </div>
      <AdminCard
        title={doc.name}
        sub={`${doc.type} · version ${doc.version}${doc.zone_name ? ` · ${doc.zone_name}` : " · no zone yet"}`}
        action={<DocumentActions doc={doc} onNewVersion={() => setVersionKey(Date.now())} />}
      >
        <dl className="docfacts">
          <div>
            <dt>Status</dt>
            <dd>
              <StatusChip tone={status.tone}>{status.label}</StatusChip>
              {!doc.is_current_version && (
                <span className="fmeta">
                  {" "}
                  earlier version{current ? (
                    <>
                      {" · "}
                      <Link href={documentHref(current.id)}>current is v{current.version}</Link>
                    </>
                  ) : null}
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>Pipeline</dt>
            <dd>
              <StatusChip tone={state.tone}>{state.label}</StatusChip>
            </dd>
          </div>
          <div>
            <dt>Review</dt>
            <dd className="mono">
              {review && review.total > 0 ? (
                <>
                  {review.pending} pending · {review.approved} approved · {review.amended} amended · {review.rejected} rejected
                  {review.pending > 0 && (
                    <>
                      {" "}
                      <Link href={reviewHref(doc.id)}>open the queue →</Link>
                    </>
                  )}
                </>
              ) : (
                "nothing extracted yet"
              )}
            </dd>
          </div>
          <div>
            <dt>Coverage</dt>
            <dd>{doc.has_coverage ? (doc.coverage_live ? "Live on the map" : "Ready, not live") : "No coverage area yet"}</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd>
              {doc.source ?? "—"}
              {doc.source_url && (
                <>
                  {" · "}
                  <a href={doc.source_url} target="_blank" rel="noreferrer">
                    registry entry ↗
                  </a>
                </>
              )}
            </dd>
          </div>
          <div>
            <dt>Adopted</dt>
            <dd className="mono">{doc.adopted_on ?? "—"}</dd>
          </div>
          <div className="wide">
            <dt>Licence / permission</dt>
            <dd>{doc.licence_note ?? "Not recorded"}</dd>
          </div>
          <div>
            <dt>Registered</dt>
            <dd className="mono">
              {doc.registered_at ? `${utcStamp(doc.registered_at)} · ${doc.registered_by ?? ""}` : "seeded"}
            </dd>
          </div>
        </dl>
      </AdminCard>

      <AdminCard
        title="Files"
        sub="Each PDF is read on its own: queued → extracting → ready for review"
      >
        {doc.is_current_version ? (
          <div className="filedrop">
            <div className="addas" role="radiogroup" aria-label="Add the dropped PDFs as">
              <span className="fieldlab">Add as</span>
              {FILE_ROLES.map((r) => (
                <button
                  key={r.value}
                  type="button"
                  role="radio"
                  aria-checked={addAs === r.value}
                  className={addAs === r.value ? "on" : undefined}
                  title={r.hint}
                  onClick={() => setAddAs(r.value)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            <DropZone
              accept={ACCEPT_PDF}
              title="Drop PDFs here or click to choose — several at once"
              hint={FILE_ROLES.find((r) => r.value === addAs)?.hint}
              onFiles={(files) => {
                uploads.add(files, addAs);
                void uploads.start();
              }}
            />
            <UploadList items={uploads.items} onRemove={uploads.remove} />
            {uploads.items.some((i) => i.status !== "waiting" && i.status !== "uploading") && !uploads.busy && (
              <button type="button" className="abtn sm ghost clearup" onClick={uploads.clearFinished}>
                Clear this list
              </button>
            )}
          </div>
        ) : (
          <div className="admin-note">Files belong to the current version; this is version {doc.version}.</div>
        )}
        <FilesTable doc={doc} />
      </AdminCard>

      <AdminCard title="Version history" sub="Re-registering a document creates a new version; earlier versions and their values stay">
        <table className="tbl">
          <thead>
            <tr>
              <th>Version</th>
              <th>Name</th>
              <th>Status</th>
              <th>Files</th>
              <th>Registered</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(doc.versions ?? []).map((v) => {
              const chip = statusChip(v.status);
              return (
                <tr key={v.id}>
                  <td className="mono">v{v.version}</td>
                  <td>{v.id === doc.id ? <b>{v.name ?? doc.name}</b> : <Link href={documentHref(v.id)}>{v.name ?? doc.name}</Link>}</td>
                  <td>
                    <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                  </td>
                  <td className="mono">{v.file_count ?? 0}</td>
                  <td className="mono">
                    {v.registered_at ? `${utcStamp(v.registered_at)}${v.registered_by ? ` · ${v.registered_by}` : ""}` : "seeded"}
                  </td>
                  <td>{v.is_current_version ? <StatusChip tone="ok">Current</StatusChip> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </AdminCard>

      {versionKey != null && (
        <RegisterDialog
          key={versionKey}
          open
          onOpenChange={(open) => !open && setVersionKey(null)}
          zones={zones}
          types={types}
          replaces={doc}
        />
      )}
    </>
  );
}
