"use client";

/**
 * One planning document version (`/admin/data/documents/[id]`): its registry facts (with its
 * short code and municipality; "Edit" changes the current version's status, name, short code,
 * zone, source, registry link, adoption date and licence note: `PATCH /v1/admin/documents/{id}`,
 * audited), its files and its version history.
 *
 * Files: drop PDFs (several at once) after choosing what they are read for ("Add as" text /
 * drawing / both); as drawings, GIS files too (a QGIS redraw of scanned sheets, the plan's GIS). Each file uploads with a progress bar, joins the version and, unless it is a
 * drawing, goes straight to the AI extraction; its row then moves queued → extracting → ready for
 * review (or failed, with the reason) while the page re-reads its data every 3 s, and polling
 * stops once every file is finished. The same PDF dropped again is recognised by its checksum:
 * "already uploaded", the existing row, no new one. Per file: pages, the pages to redraw in QGIS
 * (scanned sheets by the week-1 assessment's rule), extraction
 * state and cost, the geometry job, rerun / retry, the review queue filtered to the file, and
 * "Remove" (refused once an item read from it was approved).
 */
import Link from "next/link";
import { useState, useTransition } from "react";

import {
  ACCEPT_DRAWING,
  ACCEPT_PDF,
  documentActive,
  documentHref,
  DOCUMENT_STATUSES,
  extractionPill,
  FILE_ROLES,
  formatCost,
  jobPill,
  kindsForRole,
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
  updateDocumentAction,
  type DocumentEdit,
} from "@/lib/admin/data-actions";
import { relativeTime, utcStamp } from "@/lib/admin/format";
import type { AdminDocument, AdminDocumentFile, AdminGeoreference, DocumentStatus, FileRole } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, type ChipTone, StatusChip } from "../parts";

import { DocumentActions, FileActions } from "./data-screen";
import { ActionButton, AutoRefresh, DataReadOnly, PillView, useDataReadOnly } from "./parts";
import { RegisterDialog, type TypeOption, type ZoneOption } from "./register-dialog";
import { DropZone, UploadList, useUploads } from "./uploads";

function RoleSelect({ doc, file }: { doc: AdminDocument; file: AdminDocumentFile }) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  const readOnly = useDataReadOnly();
  if (readOnly) return <>{FILE_ROLES.find((r) => r.value === file.role)?.label ?? file.role}</>;
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
        No files yet. Drop the document&apos;s PDFs above: text parts are read by the AI extraction, drawings (plan sheets,
        or the plan as a GIS file) by the geometry job.
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
          <th>QGIS redraw</th>
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
              <td
                className="mono"
                title={
                  file.kind !== "planning_document"
                    ? "A GIS file is geometry already"
                    : file.redraw_pages == null
                      ? "The pages are read with the extraction or the geometry job"
                      : file.redraw_pages.length
                        ? "Scanned sheets (the week-1 assessment's class C): redraw them in QGIS and add the GeoPackage as a drawing"
                        : "No scanned sheet"
                }
              >
                {file.kind !== "planning_document" ? (
                  "—"
                ) : file.redraw_pages == null ? (
                  "not read yet"
                ) : file.redraw_pages.length ? (
                  <span className="redraw">p. {file.redraw_pages.join(", ")}</span>
                ) : (
                  "none"
                )}
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

const GEOREF_STATUS: Record<AdminGeoreference["status"], { tone: ChipTone; label: string }> = {
  staged: { tone: "pend", label: "Staged · publish to serve" },
  published: { tone: "ok", label: "Published" },
  invalid: { tone: "rev", label: "Refused by the validation" },
  superseded: { tone: "pend", label: "Superseded" },
};

function metres(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(3)} m`;
}

function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${(value * 100).toFixed(1)} %`;
}

/** The residual report of the latest georeferencing run: RMSE per sheet against the document's
 * threshold, the snapping to the cadastral base and the validation (core.gis.georef). */
function GeoreferenceCard({ geo }: { geo: AdminGeoreference | null | undefined }) {
  if (!geo) {
    return (
      <AdminCard title="Georeferencing" sub="Control points on the sheets fitted to the plan's coordinate system">
        <div className="admin-note">
          Not georeferenced yet. Control points, the fit and its residuals are prepared with{" "}
          <span className="mono">python -m core.gis.georef</span> (grid / add → fit → apply --stage); the result shows here.
        </div>
      </AdminCard>
    );
  }
  const status = GEOREF_STATUS[geo.status];
  const limit = geo.max_rmse_m ?? null;
  const native = geo.method === "native";
  const sub = native
    ? `GIS drawing in its own coordinate system (${geo.crs}) · reprojected, no control points needed`
    : `${geo.method === "helmert" ? "Helmert" : "Affine"} fit to ${geo.crs} · ${geo.points_used} control points${geo.source === "manual_redraw" ? " · redrawn sheets" : ""}`;
  return (
    <AdminCard title="Georeferencing" sub={sub}>
      <dl className="docfacts">
        <div>
          <dt>Status</dt>
          <dd>
            <StatusChip tone={status.tone}>{status.label}</StatusChip>
          </dd>
        </div>
        <div>
          <dt>RMSE</dt>
          <dd className="mono">
            {native ? (
              "no fit: the file carries its coordinates"
            ) : (
              <>
                {metres(geo.rmse_m)}
                {limit != null ? ` (limit ${limit} m)` : ""} · max residual {metres(geo.max_residual_m)}
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Snapping</dt>
          <dd className="mono">
            {geo.snap_tolerance_m ? (
              <>
                {percent(geo.snapped_ratio)} of vertices within {geo.snap_tolerance_m} m · {geo.near_misses ?? 0} near misses
              </>
            ) : (
              "off"
            )}
          </dd>
        </div>
        <div>
          <dt>On the cadastre</dt>
          <dd className="mono">
            {percent(geo.cadastral_overlap_share)} of the planned parcel area
            {geo.systematic_offset_m != null ? ` · mean offset ${metres(geo.systematic_offset_m)}` : ""}
          </dd>
        </div>
        <div>
          <dt>Dataset</dt>
          <dd className="mono">
            {geo.dataset_version} · {utcStamp(geo.created_at)}
            {geo.published_at ? ` · published ${utcStamp(geo.published_at)}` : ""}
          </dd>
        </div>
        {(geo.errors?.length ?? 0) + (geo.warnings?.length ?? 0) > 0 && (
          <div className="wide">
            <dt>Validation</dt>
            <dd className="mono">{[...(geo.errors ?? []), ...(geo.warnings ?? [])].join(" · ")}</dd>
          </div>
        )}
      </dl>
      {(geo.sheets ?? []).length > 0 && (
      <table className="tbl">
        <thead>
          <tr>
            <th>Sheet</th>
            <th>Page</th>
            <th>Control points</th>
            <th>RMSE</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(geo.sheets ?? []).map((s) => (
            <tr key={s.sheet}>
              <td className="mono">{s.sheet}</td>
              <td className="mono">{s.page ?? "—"}</td>
              <td className="mono">{s.points}</td>
              <td className="mono">{metres(s.rmse_m)}</td>
              <td>
                {s.rmse_m == null ? (
                  <StatusChip tone="pend">No points</StatusChip>
                ) : limit == null || s.rmse_m <= limit ? (
                  <StatusChip tone="ok">Within limit</StatusChip>
                ) : (
                  <StatusChip tone="rev">Above limit</StatusChip>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}
    </AdminCard>
  );
}

function editOf(doc: AdminDocument): DocumentEdit {
  return {
    name: doc.name,
    shortCode: doc.short_code ?? "",
    status: doc.status,
    zoneId: doc.zone_id ?? null,
    source: doc.source ?? "",
    sourceUrl: doc.source_url ?? "",
    adoptedOn: doc.adopted_on ?? "",
    licenceNote: doc.licence_note ?? "",
  };
}

/** "Edit": the current version's registry facts (`PATCH /v1/admin/documents/{id}`, audited). */
function EditDocument({ doc, zones, onClose }: { doc: AdminDocument; zones: ZoneOption[]; onClose: () => void }) {
  const showToast = useShell((s) => s.showToast);
  const [form, setForm] = useState<DocumentEdit>(() => editOf(doc));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const set = <K extends keyof DocumentEdit>(key: K, value: DocumentEdit[K]) => setForm((f) => ({ ...f, [key]: value }));
  const statusChanged = form.status !== doc.status;
  const save = () =>
    start(async () => {
      const result = await updateDocumentAction(doc.id, form);
      if (!result.ok) {
        setErrors(result.fields ?? {});
        setMessage(result.message);
        return;
      }
      showToast(result.message);
      onClose();
    });
  return (
    <form
      className="regform docedit"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <div className="frow">
        <div className="field">
          <label htmlFor="edit-name">Official name</label>
          <input id="edit-name" value={form.name} maxLength={300} onChange={(e) => set("name", e.target.value)} />
          {errors.name && <div className="ferr">{errors.name}</div>}
        </div>
        <div className="field">
          <label htmlFor="edit-code">
            Short code <span className="opt">optional</span>
          </label>
          <input
            id="edit-code"
            value={form.shortCode}
            maxLength={40}
            placeholder="DUP-NG12"
            onChange={(e) => set("shortCode", e.target.value)}
          />
          {errors.short_code && <div className="ferr">{errors.short_code}</div>}
        </div>
      </div>
      <div className="frow">
        <div className="field">
          <label htmlFor="edit-status">Status</label>
          <select id="edit-status" value={form.status} onChange={(e) => set("status", e.target.value as DocumentStatus)}>
            {DOCUMENT_STATUSES.map((st) => (
              <option key={st.value} value={st.value}>
                {st.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="edit-zone">Zone</label>
          <select
            id="edit-zone"
            value={form.zoneId ?? ""}
            onChange={(e) => set("zoneId", e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">Not assigned</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id}>
                {z.name}
              </option>
            ))}
          </select>
          {errors.zone_id && <div className="ferr">{errors.zone_id}</div>}
        </div>
        <div className="field">
          <label htmlFor="edit-adopted">
            Adoption date <span className="opt">optional</span>
          </label>
          <input
            id="edit-adopted"
            type="date"
            value={form.adoptedOn}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(e) => set("adoptedOn", e.target.value)}
          />
          {errors.adopted_on && <div className="ferr">{errors.adopted_on}</div>}
        </div>
      </div>
      <div className="frow">
        <div className="field">
          <label htmlFor="edit-source">Source</label>
          <input id="edit-source" value={form.source} maxLength={200} onChange={(e) => set("source", e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="edit-url">
            Registry link <span className="opt">optional</span>
          </label>
          <input
            id="edit-url"
            type="url"
            value={form.sourceUrl}
            maxLength={1000}
            placeholder="https://lamp.gov.me/…"
            onChange={(e) => set("sourceUrl", e.target.value)}
          />
        </div>
      </div>
      <div className="field">
        <label htmlFor="edit-licence">
          Licence / permission note <span className="opt">optional</span>
        </label>
        <textarea
          id="edit-licence"
          rows={2}
          value={form.licenceNote}
          maxLength={2000}
          onChange={(e) => set("licenceNote", e.target.value)}
        />
      </div>
      {statusChanged && (
        <div className="admin-note">
          {form.status === "adopted"
            ? "Adopted: location and the panels use the document at once where its coverage is live; the map tiles follow at the next publish."
            : "No longer adopted: location and the panels stop using it at once; the map tiles follow at the next publish."}
        </div>
      )}
      {message && (
        <p className="formmsg" role="alert">
          {message}
        </p>
      )}
      <div className="rowacts">
        <button type="button" className="abtn sm ghost" onClick={onClose} disabled={pending}>
          Cancel
        </button>
        <button type="submit" className="abtn sm" disabled={pending}>
          {pending ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

export function DocumentDetail({
  doc,
  zones,
  types,
  municipalityName,
  readOnly = false,
}: {
  doc: AdminDocument;
  zones: ZoneOption[];
  types: TypeOption[];
  /** The municipality's name (the profile's); the document carries its id. */
  municipalityName?: string | null;
  /** Reviewers: the document, its files and jobs without the write controls. */
  readOnly?: boolean;
}) {
  const [addAs, setAddAs] = useState<FileRole>("text");
  const [versionKey, setVersionKey] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const uploads = useUploads({
    kinds: kindsForRole(addAs),
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
    <DataReadOnly.Provider value={readOnly}>
      <AutoRefresh active={documentActive(doc) || uploads.busy} />
      <div className="crumbs">
        <Link href="/admin/data">← Data sources</Link>
      </div>
      <AdminCard
        title={doc.name}
        sub={`${doc.short_code ? `${doc.short_code} · ` : ""}${doc.type} · version ${doc.version}${doc.zone_name ? ` · ${doc.zone_name}` : " · no zone yet"}`}
        action={
          <div className="rowacts">
            <DocumentActions doc={doc} onNewVersion={() => setVersionKey(Date.now())} />
            {!readOnly && doc.is_current_version && !editing && (
              <button type="button" className="abtn sm ghost" onClick={() => setEditing(true)}>
                Edit
              </button>
            )}
          </div>
        }
      >
        {editing && <EditDocument key={doc.id} doc={doc} zones={zones} onClose={() => setEditing(false)} />}
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
          <div>
            <dt>Short code</dt>
            <dd className="mono">{doc.short_code ?? "—"}</dd>
          </div>
          <div>
            <dt>Municipality</dt>
            <dd>{municipalityName ?? doc.municipality_id}</dd>
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
        {readOnly ? null : doc.is_current_version ? (
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
              accept={addAs === "drawing" ? ACCEPT_DRAWING : ACCEPT_PDF}
              title={
                addAs === "drawing"
                  ? "Drop drawings here — plan-sheet PDFs or GIS files (.gpkg, .geojson, zipped Shapefile)"
                  : "Drop PDFs here or click to choose — several at once"
              }
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

      <GeoreferenceCard geo={doc.georeference} />

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
      {readOnly && <div className="finfoot">Read only: administrators add files, run the jobs and register new versions.</div>}
    </DataReadOnly.Provider>
  );
}
