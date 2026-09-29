"use server";

/**
 * Server actions of the AI review queue. Every call goes to the staff API with the signed-in
 * member's bearer token (the API audits each decision: actor, before / after, the note). They
 * answer data, not a page refresh: the queue keeps its own state so a reviewer moves through
 * hundreds of items without the page re-rendering; a decision answers the updated item and the
 * document's counters. Refusals come back as `{ok: false, message}` in plain words.
 *
 * Publishing is for admins and reviewers (the pilot scope's "review and publish", A4), checked
 * again on the server as the API does.
 */
import { unstable_rethrow } from "next/navigation";

import type {
  AdminJob,
  AuditPage,
  PublishStatus,
  ReviewCounters,
  ReviewItem,
  ReviewOptions,
  ReviewPage,
} from "@/lib/api/types";

import { adminGet, adminSend } from "./api";
import { correctionRefusal, explainReviewProblem, QUEUE_PAGE, reviewQuery, type ReviewFilters } from "./review";
import { canOpen } from "./sections";
import { currentStaff } from "./session";

/** A refusal may name the contract rule a correction broke (`code`, e.g. `out_of_range`). */
export type Result<T> = { ok: true; message: string; data: T } | { ok: false; message: string; code?: string };

async function reviewer(): Promise<string | null> {
  const staff = await currentStaff();
  return staff && canOpen(staff.role, "review") ? null : "Your role cannot review extracted values.";
}

async function publisher(): Promise<string | null> {
  const staff = await currentStaff();
  return staff && canOpen(staff.role, "publish") ? null : "Your role cannot publish.";
}

async function counters(documentId: number): Promise<ReviewCounters | null> {
  try {
    const rows = await adminGet<ReviewCounters[]>("/v1/admin/review/summary", { document_id: documentId });
    return rows[0] ?? null;
  } catch (err) {
    unstable_rethrow(err);
    return null;
  }
}

export interface Decision {
  item: ReviewItem;
  counters: ReviewCounters | null;
}

async function decide(
  path: string,
  body: unknown,
  message: string,
): Promise<Result<Decision>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<ReviewItem>("POST", path, body);
  if (!result.ok) return { ok: false, message: explainReviewProblem(result), code: correctionRefusal(result)?.code };
  return { ok: true, message, data: { item: result.data, counters: await counters(result.data.source.document_id) } };
}

/** `POST /v1/admin/review/{id}/approve`: the AI value as extracted. */
export async function approveAction(itemId: number, note?: string): Promise<Result<Decision>> {
  return decide(`/v1/admin/review/${itemId}/approve`, note ? { note } : {}, "Approved");
}

/** `POST /v1/admin/review/{id}/amend`: a corrected value and the note; the API checks it with the
 * extraction contract's rules (`confirm` keeps a value outside the field's usual range). */
export async function amendAction(
  itemId: number,
  correction: { value: number | string; unit: string | null; note: string; confirm?: boolean },
): Promise<Result<Decision>> {
  if (!correction.note.trim()) return { ok: false, message: "Say what was wrong: a note is required with a correction." };
  return decide(
    `/v1/admin/review/${itemId}/amend`,
    {
      value: correction.value,
      unit: correction.unit,
      note: correction.note.trim(),
      ...(correction.confirm ? { confirm_out_of_range: true } : {}),
    },
    "Amended",
  );
}

/** `POST /v1/admin/review/{id}/reject`: the value stays out; the reason is required. */
export async function rejectAction(itemId: number, note: string): Promise<Result<Decision>> {
  if (!note.trim()) return { ok: false, message: "Give the reason for rejecting." };
  return decide(`/v1/admin/review/${itemId}/reject`, { note: note.trim() }, "Rejected");
}

/** The next page of the queue for the same filters. */
export async function loadQueueAction(filters: ReviewFilters, offset: number): Promise<Result<ReviewPage>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    const page = await adminGet<ReviewPage>("/v1/admin/review", reviewQuery(filters, Math.max(0, offset)));
    return { ok: true, message: `${page.items.length} more of ${page.total}`, data: page };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: `The next ${QUEUE_PAGE} items could not be loaded. Try again in a moment.` };
  }
}

/** One item again: a freshly signed page link (they expire) and its current decision. */
export async function itemAction(itemId: number): Promise<Result<ReviewItem>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    return { ok: true, message: "", data: await adminGet<ReviewItem>(`/v1/admin/review/${itemId}`) };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: "The item could not be loaded." };
  }
}

/** The wordings a document already uses for a text field (the land-use select). */
export async function optionsAction(documentId: number, fieldKey: string): Promise<Result<ReviewOptions>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    const data = await adminGet<ReviewOptions>("/v1/admin/review/options", { document_id: documentId, field_key: fieldKey });
    return { ok: true, message: "", data };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: "The document's wordings could not be loaded; type the value instead." };
  }
}

/** `GET /v1/admin/publish`: the current data version, the active or last job, what blocks. */
export async function publishStatusAction(): Promise<Result<PublishStatus>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    return { ok: true, message: "", data: await adminGet<PublishStatus>("/v1/admin/publish") };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: "The publish status could not be loaded." };
  }
}

/** `POST /v1/admin/publish`: one publish job (every document's approved values). */
export async function publishAction(label?: string, notes?: string): Promise<Result<AdminJob>> {
  const denied = await publisher();
  if (denied) return { ok: false, message: denied };
  const body: { label?: string; notes?: string } = {};
  if (label?.trim()) body.label = label.trim();
  if (notes?.trim()) body.notes = notes.trim();
  const result = await adminSend<AdminJob>("POST", "/v1/admin/publish", body);
  if (!result.ok) {
    const details = result.details as
      | { documents?: { document_name: string; pending: number }[]; geometry?: { batch_id: number }[] }
      | undefined;
    if (result.status === 409 && (details?.documents?.length || details?.geometry?.length)) {
      const parts = (details.documents ?? []).slice(0, 3).map((d) => `${d.document_name} (${d.pending} pending)`);
      const batches = details.geometry?.length ?? 0;
      if (batches) parts.push(`${batches} geometry batch${batches === 1 ? "" : "es"} to review`);
      return { ok: false, message: `Publishing waits for: ${parts.join(", ")}` };
    }
    return { ok: false, message: explainReviewProblem(result) };
  }
  return { ok: true, message: result.status === 202 ? "Publishing started" : "A publish is already running", data: result.data };
}


/** The item's audit trail (`GET /v1/admin/audit`, entity `extraction_item`): every decision. */
export async function historyAction(itemId: number): Promise<Result<AuditPage>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    const data = await adminGet<AuditPage>("/v1/admin/audit", { entity_type: "extraction_item", entity_id: itemId, limit: 20 });
    return { ok: true, message: "", data };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: "The history could not be loaded." };
  }
}
