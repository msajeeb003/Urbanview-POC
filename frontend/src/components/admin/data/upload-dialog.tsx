"use client";

/**
 * "+ Upload document" (the Data sources card's action): drop PDF planning documents, GIS files and
 * cadastral extracts, check the kind each file was taken for (by its extension), upload them with
 * a progress bar each, and see what happened to each: uploaded, already uploaded (the checksum
 * matched a stored file: its record, never an error) or refused with the API's reason. PDFs and
 * GIS drawings can go straight on to "Register a planning document" with the files preselected.
 */
import { Cta } from "@/components/ui/cta";
import { Modal, ModalHead } from "@/components/ui/modal";
import { ACCEPT_ANY } from "@/lib/admin/data";

import type { PickedFile } from "./register-dialog";
import { DropZone, UploadList, useUploads } from "./uploads";

export function UploadDialog({
  open,
  onOpenChange,
  onRegister,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Continue to the registration form with these uploaded PDFs. */
  onRegister: (files: PickedFile[]) => void;
}) {
  const uploads = useUploads();
  const waiting = uploads.items.filter((i) => i.status === "waiting" && i.kind).length;
  const finished = uploads.items.filter((i) => i.status === "done" || i.status === "duplicate");
  // PDFs join as text (their role can change in the form), GIS files as drawings
  const pdfs: PickedFile[] = finished
    .filter((i) => i.stored?.kind === "planning_document" || i.stored?.kind === "gis")
    .map((i) => ({
      fileId: i.stored!.id,
      name: i.stored!.original_filename,
      kind: i.stored!.kind,
      role: i.stored!.kind === "gis" ? "drawing" : "text",
    }));
  const failed = uploads.items.filter((i) => i.status === "error").length;
  const summary = uploads.busy
    ? "Uploading…"
    : uploads.items.length
      ? `${finished.length} uploaded${failed ? ` · ${failed} not uploaded` : ""}${waiting ? ` · ${waiting} ready` : ""}`
      : "Files are stored privately and recognised by their checksum";

  const close = (next: boolean) => {
    if (!next && uploads.busy) return; // finish the uploads first
    if (!next) uploads.reset();
    onOpenChange(next);
  };

  return (
    <Modal open={open} onOpenChange={close} wide label="Upload documents">
      <ModalHead
        eyebrow="Data sources"
        title="Upload documents"
        lead="Planning document PDFs, GIS files and cadastral extracts. A file uploaded before is recognised and not stored twice."
      />
      <div className="mbody">
        <DropZone
          accept={ACCEPT_ANY}
          onFiles={(files) => uploads.add(files)}
          hint="PDF planning documents · GIS files (.zip, .geojson, .gpkg, .dxf, .kml) · cadastral extracts (.csv, .xlsx)"
        />
        <UploadList items={uploads.items} showKind onKind={uploads.setKind} onRemove={uploads.remove} />
      </div>
      <div className="mfoot">
        <span className="fnote">{summary}</span>
        <Cta variant="ghost" style={{ width: "auto", padding: "0 18px" }} disabled={uploads.busy} onClick={() => close(false)}>
          Done
        </Cta>
        {waiting > 0 && (
          <Cta variant="primary" disabled={uploads.busy} onClick={() => void uploads.start()}>
            Upload {waiting} file{waiting > 1 ? "s" : ""}
          </Cta>
        )}
        {waiting === 0 && pdfs.length > 0 && (
          <Cta
            variant="primary"
            disabled={uploads.busy}
            onClick={() => {
              onRegister(pdfs);
              uploads.reset();
            }}
          >
            Register a planning document →
          </Cta>
        )}
      </div>
    </Modal>
  );
}
