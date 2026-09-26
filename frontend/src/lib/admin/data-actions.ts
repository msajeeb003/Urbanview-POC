"use server";

/**
 * Server actions of the "Data sources" screen: every write goes to the staff pipeline API with the
 * signed-in member's bearer token (the API audits each one), then the screen's data is re-read in
 * the same round trip (`revalidatePath`). A refusal comes back as `{ok: false, message}` in plain
 * language (and the field errors of a 422), never as a thrown error, so the forms keep what was
 * typed and the page shows a toast.
 *
 * Uploads are not actions (server actions cap request bodies): the browser sends each file to
 * `/api/admin/files`, which streams it to `POST /v1/admin/files` and reports progress on the way.
 */
import { revalidatePath } from "next/cache";

import type { AdminDocument, AdminJob, DocumentStatus, FileRole } from "@/lib/api/types";

import { adminSend } from "./api";
import { explainProblem, fieldErrors } from "./data";
import { canOpen, isReadOnly } from "./sections";
import { currentStaff } from "./session";

export type ActionResult<T = undefined> =
  | { ok: true; message: string; data: T }
  | { ok: false; message: string; fields?: Record<string, string> };

async function allowed(): Promise<string | null> {
  const staff = await currentStaff();
  if (!staff || !canOpen(staff.role, "data") || isReadOnly(staff.role, "data")) {
    return "Your role cannot change data sources.";
  }
  return null;
}

function refresh(): void {
  revalidatePath("/admin/data", "layout");
}

export interface RegisterInput {
  name: string;
  type: string;
  status: DocumentStatus;
  source: string;
  sourceUrl: string;
  zoneId: number | null;
  adoptedOn: string;
  licenceNote: string;
  files: { fileId: number; role: FileRole }[];
  /** Register a new version of this (current) document. */
  replacesDocumentId?: number | null;
}

/** `POST /v1/admin/documents`: a new document, or the next version of one. */
export async function registerDocumentAction(input: RegisterInput): Promise<ActionResult<{ id: number }>> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const fields: Record<string, string> = {};
  if (!input.name.trim()) fields.name = "Give the document its official name.";
  if (!input.type) fields.type = "Choose the document type.";
  if (!input.status) fields.status = "Choose the status.";
  if (Object.keys(fields).length) return { ok: false, message: "Some fields need attention.", fields };

  const result = await adminSend<AdminDocument>("POST", "/v1/admin/documents", {
    name: input.name.trim(),
    type: input.type,
    status: input.status,
    source: input.source.trim() || null,
    source_url: input.sourceUrl.trim() || null,
    zone_id: input.zoneId,
    adopted_on: input.adoptedOn || null,
    licence_note: input.licenceNote.trim() || null,
    files: input.files.map((f) => ({ file_id: f.fileId, role: f.role })),
    replaces_document_id: input.replacesDocumentId ?? null,
  });
  if (!result.ok) {
    const fields = result.status === 422 ? fieldErrors(result.details) : undefined;
    return {
      ok: false,
      message: fields && Object.keys(fields).length ? "Some fields need attention." : explainProblem(result),
      fields,
    };
  }
  refresh();
  const doc = result.data;
  return {
    ok: true,
    message: doc.version > 1 ? `Version ${doc.version} of ${doc.name} registered` : `${doc.name} registered`,
    data: { id: doc.id },
  };
}

/** `POST /v1/admin/documents/{id}/files`: put uploaded files on the current version. */
export async function attachFilesAction(
  documentId: number,
  files: { fileId: number; role: FileRole }[],
): Promise<ActionResult<{ added: boolean }>> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<AdminDocument>("POST", `/v1/admin/documents/${documentId}/files`, {
    files: files.map((f) => ({ file_id: f.fileId, role: f.role })),
  });
  if (!result.ok) return { ok: false, message: explainProblem(result) };
  refresh();
  const added = result.status === 201;
  return { ok: true, message: added ? "Added to the document" : "Already on the document", data: { added } };
}

/** `PATCH /v1/admin/documents/{id}/files/{file_id}`: what the file is read for. */
export async function setFileRoleAction(documentId: number, fileId: number, role: FileRole): Promise<ActionResult> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<AdminDocument>("PATCH", `/v1/admin/documents/${documentId}/files/${fileId}`, { role });
  if (!result.ok) return { ok: false, message: explainProblem(result) };
  refresh();
  return { ok: true, message: "Role changed", data: undefined };
}

/** `DELETE /v1/admin/documents/{id}/files/{file_id}`: take a file off the version. */
export async function removeFileAction(documentId: number, fileId: number): Promise<ActionResult> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<AdminDocument>("DELETE", `/v1/admin/documents/${documentId}/files/${fileId}`);
  if (!result.ok) return { ok: false, message: explainProblem(result) };
  refresh();
  return { ok: true, message: "File removed from the document", data: undefined };
}

/**
 * `POST /v1/admin/documents/{id}/jobs/extract` for each file (default: every text / both PDF of
 * the version). Idempotent on the API side: a file already read the same way answers its run.
 */
export async function queueExtractionAction(
  documentId: number,
  fileIds: number[] | null,
  force = false,
): Promise<ActionResult<{ queued: number; reused: number }>> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const targets: (number | undefined)[] = fileIds && fileIds.length ? fileIds : [undefined];
  let queued = 0;
  let reused = 0;
  for (const fileId of targets) {
    const result = await adminSend<AdminJob>(
      "POST",
      `/v1/admin/documents/${documentId}/jobs/extract`,
      undefined,
      { file_id: fileId, force: force || undefined },
    );
    if (!result.ok) {
      refresh();
      return { ok: false, message: explainProblem(result) };
    }
    if (result.status === 202) queued += 1;
    else reused += 1;
  }
  refresh();
  const message =
    queued && reused
      ? `Extraction queued for ${queued} file${queued > 1 ? "s" : ""}; ${reused} already read`
      : queued
        ? `Extraction queued${queued > 1 ? ` for ${queued} files` : ""}`
        : "Already extracted the same way — nothing new to read";
  return { ok: true, message, data: { queued, reused } };
}

/** `POST /v1/admin/files/{id}/jobs/geo` for each file. */
export async function queueGeometryAction(fileIds: number[]): Promise<ActionResult<{ queued: number }>> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  let queued = 0;
  for (const fileId of fileIds) {
    const result = await adminSend<AdminJob>("POST", `/v1/admin/files/${fileId}/jobs/geo`);
    if (!result.ok) {
      refresh();
      return { ok: false, message: explainProblem(result) };
    }
    if (result.status === 202) queued += 1;
  }
  refresh();
  return {
    ok: true,
    message: queued ? `Geometry job queued${queued > 1 ? ` for ${queued} files` : ""}` : "Already queued",
    data: { queued },
  };
}

/** `POST /v1/admin/jobs/{id}/retry`: a failed job again (an extraction resumes its finished steps). */
export async function retryJobAction(jobId: number): Promise<ActionResult> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<AdminJob>("POST", `/v1/admin/jobs/${jobId}/retry`);
  if (!result.ok) return { ok: false, message: explainProblem(result) };
  refresh();
  return { ok: true, message: "Job queued again", data: undefined };
}

/** `PATCH /v1/admin/documents/{id}/coverage`: the coverage area takes part in location or not. */
export async function setCoverageLiveAction(documentId: number, live: boolean): Promise<ActionResult> {
  const denied = await allowed();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<AdminDocument>("PATCH", `/v1/admin/documents/${documentId}/coverage`, { live });
  if (!result.ok) return { ok: false, message: explainProblem(result) };
  refresh();
  return { ok: true, message: live ? "Coverage is live on the map" : "Coverage taken off the map", data: undefined };
}
