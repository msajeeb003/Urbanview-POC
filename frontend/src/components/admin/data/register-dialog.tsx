"use client";

/**
 * "Register a planning document" (and "New version of …"): the form behind
 * `POST /v1/admin/documents`. Name, short code (optional, "DUP-NG12"; a new version keeps the
 * previous one), type (the profile's document types, DUP / PUP / PGR first),
 * status (adopted / in progress / superseded), source (default eRegistri) and its link, zone, the
 * adoption date, the licence / permission note, and the document's files: the PDFs just uploaded
 * (each read as text, drawing or both) and GIS drawings (a QGIS redraw, the plan's GIS: drawing
 * only), plus any dropped here. A new version is registered against
 * the current one (`replaces_document_id`): the form starts from its values and shows the version
 * history. On success a toast and the document's page; on failure the form keeps everything typed
 * and says what to fix.
 */
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { Cta } from "@/components/ui/cta";
import { Modal, ModalHead } from "@/components/ui/modal";
import { ACCEPT_DRAWING, documentHref, DOCUMENT_STATUSES, FILE_ROLES, rolesFor, statusChip } from "@/lib/admin/data";
import { registerDocumentAction, type RegisterInput } from "@/lib/admin/data-actions";
import { useStaffZone } from "@/lib/admin/use-staff-zone";
import type { AdminDocument, DocumentStatus, FileRole } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { StatusChip } from "../parts";

import { DropZone, UploadList, useUploads } from "./uploads";

export interface PickedFile {
  fileId: number;
  name: string;
  kind: string;
  role: FileRole;
}

export interface ZoneOption {
  id: number;
  name: string;
}

export interface TypeOption {
  value: string;
  label: string;
}

function initialState(replaces: AdminDocument | null | undefined, types: TypeOption[]) {
  return {
    name: replaces?.name ?? "",
    shortCode: replaces?.short_code ?? "",
    type: replaces?.type ?? types[0]?.value ?? "DUP",
    status: (replaces?.status ?? "adopted") as DocumentStatus,
    source: replaces?.source ?? "eRegistri",
    sourceUrl: replaces?.source_url ?? "",
    zoneId: replaces?.zone_id ?? null,
    adoptedOn: replaces?.adopted_on ?? "",
    licenceNote: replaces?.licence_note ?? "",
  };
}

export function RegisterDialog({
  open,
  onOpenChange,
  zones,
  types,
  initialFiles = [],
  replaces,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  zones: ZoneOption[];
  types: TypeOption[];
  initialFiles?: PickedFile[];
  replaces?: AdminDocument | null;
}) {
  const router = useRouter();
  const showToast = useShell((s) => s.showToast);
  const { named } = useStaffZone();
  const [form, setForm] = useState(() => initialState(replaces, types));
  const [files, setFiles] = useState<PickedFile[]>(initialFiles);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const uploads = useUploads({
    kinds: ["planning_document", "gis"],
    onUploaded: (item) => {
      const stored = item.stored!;
      setFiles((list) =>
        list.some((f) => f.fileId === stored.id)
          ? list
          : [...list, { fileId: stored.id, name: stored.original_filename, kind: stored.kind, role: item.role }],
      );
      return item.status === "duplicate" ? "added to the list below" : "added to the list below";
    },
  });

  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setErrors((e) => {
      if (!(key in e)) return e;
      const next = { ...e };
      delete next[key as string];
      return next;
    });
  };

  const submit = () => {
    if (uploads.busy) {
      setMessage("Wait for the uploads to finish.");
      return;
    }
    const input: RegisterInput = {
      ...form,
      files: files.map((f) => ({ fileId: f.fileId, role: f.role })),
      replacesDocumentId: replaces?.id ?? null,
    };
    start(async () => {
      try {
        const result = await registerDocumentAction(input);
        if (!result.ok) {
          setErrors(result.fields ?? {});
          setMessage(result.message);
          return;
        }
        showToast(result.message);
        onOpenChange(false);
        router.push(documentHref(result.data.id));
      } catch {
        setMessage("The data service did not answer. Nothing was registered; try again.");
      }
    });
  };

  const versions = replaces?.versions ?? [];
  const nextVersion = (replaces?.version ?? 0) + 1;

  return (
    <Modal open={open} onOpenChange={onOpenChange} wide label="Register a planning document">
      <ModalHead
        eyebrow={replaces ? `New version · v${nextVersion}` : "Data sources"}
        title={replaces ? `New version of ${replaces.name}` : "Register a planning document"}
        lead={
          replaces
            ? `Version ${nextVersion} replaces version ${replaces.version}: the old version is kept, its coverage leaves the map until the new version's geometry is live.`
            : "The document's registry facts and its files. Extraction starts from the document's page."
        }
      />
      <form
        className="mbody regform"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="field">
          <label htmlFor="doc-name">Official name</label>
          <input
            id="doc-name"
            value={form.name}
            maxLength={300}
            placeholder="DUP Centar — izmjene i dopune"
            aria-invalid={!!errors.name || undefined}
            onChange={(e) => set("name", e.target.value)}
          />
          {errors.name && <div className="ferr">{errors.name}</div>}
        </div>
        <div className="frow">
          <div className="field">
            <label htmlFor="doc-code">
              Short code <span className="opt">optional</span>
            </label>
            <input
              id="doc-code"
              value={form.shortCode}
              maxLength={40}
              placeholder="DUP-NG12"
              aria-invalid={!!errors.short_code || undefined}
              onChange={(e) => set("shortCode", e.target.value)}
            />
            {errors.short_code && <div className="ferr">{errors.short_code}</div>}
          </div>
          <div className="field">
            <label htmlFor="doc-type">Type</label>
            <select id="doc-type" value={form.type} onChange={(e) => set("type", e.target.value)}>
              {types.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
            {errors.type && <div className="ferr">{errors.type}</div>}
          </div>
        </div>
        <div className="frow">
          <div className="field">
            <label htmlFor="doc-status">Status</label>
            <select
              id="doc-status"
              value={form.status}
              onChange={(e) => set("status", e.target.value as DocumentStatus)}
            >
              {DOCUMENT_STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
            {errors.status && <div className="ferr">{errors.status}</div>}
          </div>
          <div className="field">
            <label htmlFor="doc-zone">
              Zone <span className="opt">optional</span>
            </label>
            <select
              id="doc-zone"
              value={form.zoneId ?? ""}
              onChange={(e) => set("zoneId", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Not assigned yet</option>
              {zones.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}
                </option>
              ))}
            </select>
            {errors.zone_id && <div className="ferr">{errors.zone_id}</div>}
          </div>
        </div>
        <div className="frow">
          <div className="field">
            <label htmlFor="doc-adopted">
              Adoption date <span className="opt">optional</span>
            </label>
            <input
              id="doc-adopted"
              type="date"
              value={form.adoptedOn}
              max={new Date().toISOString().slice(0, 10)}
              onChange={(e) => set("adoptedOn", e.target.value)}
            />
            {errors.adopted_on && <div className="ferr">{errors.adopted_on}</div>}
          </div>
          <div className="field">
            <label htmlFor="doc-source">Source</label>
            <input id="doc-source" value={form.source} maxLength={200} onChange={(e) => set("source", e.target.value)} />
          </div>
        </div>
        <div className="frow">
          <div className="field">
            <label htmlFor="doc-url">
              Registry link <span className="opt">optional</span>
            </label>
            <input
              id="doc-url"
              type="url"
              value={form.sourceUrl}
              maxLength={1000}
              placeholder="https://lamp.gov.me/…"
              onChange={(e) => set("sourceUrl", e.target.value)}
            />
            {errors.source_url && <div className="ferr">{errors.source_url}</div>}
          </div>
        </div>
        <div className="field">
          <label htmlFor="doc-licence">
            Licence / permission note <span className="opt">optional</span>
          </label>
          <textarea
            id="doc-licence"
            rows={2}
            value={form.licenceNote}
            maxLength={2000}
            placeholder="Public planning document, published on eRegistri; reuse permitted with attribution."
            onChange={(e) => set("licenceNote", e.target.value)}
          />
          {errors.licence_note && <div className="ferr">{errors.licence_note}</div>}
        </div>

        <div className="field">
          <div className="fieldlab">Files of this {replaces ? "version" : "document"}</div>
          {files.length > 0 && (
            <ul className="pickedfiles">
              {files.map((f) => (
                <li key={f.fileId}>
                  <span className="up-name" title={f.name}>
                    {f.name}
                  </span>
                  <span className="mono up-size">#{f.fileId}</span>
                  <select
                    aria-label={`Role of ${f.name}`}
                    value={f.role}
                    onChange={(e) =>
                      setFiles((list) =>
                        list.map((x) => (x.fileId === f.fileId ? { ...x, role: e.target.value as FileRole } : x)),
                      )
                    }
                  >
                    {FILE_ROLES.filter((r) => rolesFor(f.kind).includes(r.value)).map((r) => (
                      <option key={r.value} value={r.value} title={r.hint}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="up-x"
                    aria-label={`Leave out ${f.name}`}
                    onClick={() => setFiles((list) => list.filter((x) => x.fileId !== f.fileId))}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}
          <DropZone
            accept={ACCEPT_DRAWING}
            title={files.length ? "Add more files" : "Drop the document's PDFs (and GIS drawings) here or click to choose"}
            hint="Text PDFs go to extraction, drawings to the geometry job. Files can be added later."
            onFiles={(list) => {
              uploads.add(list);
              void uploads.start();
            }}
          />
          <UploadList items={uploads.items.filter((i) => i.status !== "done" && i.status !== "duplicate")} onRemove={uploads.remove} />
          {errors.files && <div className="ferr">{errors.files}</div>}
        </div>

        {replaces && versions.length > 0 && (
          <div className="field">
            <div className="fieldlab">Version history</div>
            <table className="tbl vertbl">
              <tbody>
                {versions.map((v) => {
                  const chip = statusChip(v.status);
                  return (
                    <tr key={v.id}>
                      <td className="mono">v{v.version}</td>
                      <td>{v.name ?? replaces.name}</td>
                      <td>
                        <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                      </td>
                      <td className="mono">{v.file_count ?? 0} files</td>
                      <td className="mono">{v.registered_at ? named(v.registered_at) : "seeded"}</td>
                      <td>{v.is_current_version ? "current" : ""}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {message && (
          <p className="formmsg" role="alert">
            {message}
          </p>
        )}
        <button type="submit" hidden />
      </form>
      <div className="mfoot">
        <span className="fnote">
          {files.length} file{files.length === 1 ? "" : "s"} · registration is recorded in the audit log
        </span>
        <Cta variant="ghost" style={{ width: "auto", padding: "0 18px" }} onClick={() => onOpenChange(false)}>
          Cancel
        </Cta>
        <Cta variant="primary" disabled={pending || uploads.busy} onClick={submit}>
          {pending ? "Registering…" : replaces ? `Register version ${nextVersion}` : "Register document"}
        </Cta>
      </div>
    </Modal>
  );
}
