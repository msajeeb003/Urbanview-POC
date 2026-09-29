"use client";

/**
 * "Zones from QGIS" (Data sources, admins; the pilot scope's `POST /api/admin/zones/import`): the
 * zone GeoPackage drawn in QGIS (its `zones` layer and `zone_documents` table, the template of
 * `python -m core.zones template`) is uploaded like any file (checksum de-duplicated), then either
 * checked ("Check only": the validation, nothing staged) or imported (validated, then staged; the
 * next publish applies the zones and their documents). There is no zone editor: zones are drawn in
 * QGIS. The recent imports show what the worker answered: staged with the dataset version and the
 * counts, passed, or refused with the first errors.
 */
import { useState, useTransition } from "react";

import { ACCEPT_GEOPACKAGE, jobPill, type UploadKind } from "@/lib/admin/data";
import { importZonesAction } from "@/lib/admin/data-actions";
import { relativeTime } from "@/lib/admin/format";
import type { AdminJob, StoredFile } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard } from "../parts";

import { PillView } from "./parts";
import { DropZone, UploadList, useUploads } from "./uploads";

const GIS_ONLY: readonly UploadKind[] = ["gis"];

/** What an import job's result says, in one line. */
export function zoneImportSummary(job: AdminJob): string | null {
  const result = (job.result ?? null) as {
    status?: string;
    dataset_version?: string;
    zones?: number;
    documents?: number;
    warnings?: unknown[];
    superseded?: string[];
  } | null;
  if (!result) return null;
  const warnings = result.warnings?.length ? ` · ${result.warnings.length} warning${result.warnings.length > 1 ? "s" : ""}` : "";
  if (result.status === "valid") return `checks passed${warnings} · nothing staged`;
  if (result.status === "staged") {
    const replaced = result.superseded?.length ? ` · replaces ${result.superseded.join(", ")}` : "";
    return `${result.dataset_version}: ${result.zones} zones, ${result.documents} documents staged${warnings}${replaced} · publish to apply`;
  }
  return null;
}

export function ZoneImportCard({ imports }: { imports: AdminJob[] }) {
  const showToast = useShell((s) => s.showToast);
  const [file, setFile] = useState<StoredFile | null>(null);
  const [pending, start] = useTransition();
  const uploads = useUploads({
    kinds: GIS_ONLY,
    onUploaded: (item) => {
      const stored = item.stored!;
      if (!stored.original_filename.toLowerCase().endsWith(".gpkg")) return "not a GeoPackage";
      setFile(stored);
      return item.status === "duplicate" ? "uploaded before — ready to import" : "ready to import";
    },
  });

  const run = (dryRun: boolean) => {
    if (!file) return;
    start(async () => {
      const result = await importZonesAction(file.id, dryRun);
      showToast(result.message);
    });
  };

  return (
    <AdminCard
      title="Zones from QGIS"
      sub="The zone GeoPackage drawn in QGIS: checked, then staged; the next publish applies it. Zones are not drawn here."
    >
      <div className="zoneimport">
        <DropZone
          accept={ACCEPT_GEOPACKAGE}
          title={file ? `${file.original_filename} · #${file.id}` : "Drop the zone GeoPackage (.gpkg) here or click to choose"}
          hint="Its zones layer and zone_documents table, as the QGIS template (python -m core.zones template) has them."
          onFiles={(files) => {
            setFile(null);
            uploads.reset();
            uploads.add(files.slice(0, 1));
            void uploads.start();
          }}
        />
        <UploadList items={uploads.items} onRemove={uploads.remove} />
        <div className="rowacts">
          <button type="button" className="abtn sm ghost" disabled={!file || pending || uploads.busy} onClick={() => run(true)}>
            Check only
          </button>
          <button type="button" className="abtn sm" disabled={!file || pending || uploads.busy} onClick={() => run(false)}>
            {pending ? "Working…" : "Import zones"}
          </button>
        </div>
      </div>
      {imports.length > 0 && (
        <table className="tbl">
          <thead>
            <tr>
              <th>Import</th>
              <th>Requested</th>
              <th>Status</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {imports.map((job) => (
              <tr key={job.id}>
                <td className="mono">
                  #{job.id} · file #{job.file_id ?? job.target_id}
                  {job.payload?.dry_run ? " · check only" : ""}
                </td>
                <td className="mono">
                  {relativeTime(job.requested_at)} · {job.requested_by}
                </td>
                <td>
                  <PillView pill={{ ...jobPill(job), reason: null }} />
                </td>
                <td className="zoneoutcome">{zoneImportSummary(job) ?? jobPill(job).reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </AdminCard>
  );
}
