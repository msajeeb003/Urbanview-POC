"use server";

/**
 * Server actions of the Planning rules tab (admins): save a zone's typical planning values as a
 * new version of its zone parameter set (`POST /v1/admin/zone-parameters` for a zone without one,
 * `PUT /v1/admin/zone-parameters/{id}` from the current version; every field is sent, so a
 * cleared field is cleared), and list a zone's planning documents for the source picker.
 */
import { revalidatePath } from "next/cache";

import type { AdminDocumentList, ZoneParameterSet, ZoneParametersIn } from "@/lib/api/types";

import { adminSend } from "./api";
import type { DocumentOption } from "./rules";
import { currentStaff } from "./session";

export type RuleResult = { ok: true; message: string } | { ok: false; message: string };

function explain(problem: { status: number; message?: string; details?: unknown }): string {
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status === 409) return "Someone saved this zone's rule meanwhile: reload the page and try again.";
  if (problem.status === 0 || problem.status >= 500) return "The data service did not answer. Try again in a moment.";
  if (problem.status === 422 && Array.isArray(problem.details)) {
    const msgs = (problem.details as { msg?: string }[]).map((d) => d.msg?.replace(/^Value error, /, "")).filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return problem.message || "The rule was refused.";
}

export async function saveRuleAction(currentId: number | null, values: ZoneParametersIn): Promise<RuleResult> {
  const staff = await currentStaff();
  if (staff?.role !== "admin") return { ok: false, message: "Only admins change the planning rules." };
  const answer =
    currentId == null
      ? await adminSend<ZoneParameterSet>("POST", "/v1/admin/zone-parameters", values)
      : await adminSend<ZoneParameterSet>("PUT", `/v1/admin/zone-parameters/${currentId}`, {
          land_use: values.land_use,
          max_far: values.max_far,
          max_site_coverage_pct: values.max_site_coverage_pct,
          max_height_m: values.max_height_m,
          max_floors: values.max_floors,
          source_document_id: values.source_document_id,
          source_page: values.source_page,
          source_note: values.source_note,
          verified_on: values.verified_on,
          verified_by: values.verified_by,
          notes: values.notes,
        });
  if (!answer.ok) return { ok: false, message: explain(answer) };
  revalidatePath("/admin/rules");
  return { ok: true, message: `Rule saved — ${answer.data.zone_name ?? "zone"} v${answer.data.version}` };
}

export type DocumentsResult = { ok: true; items: DocumentOption[] } | { ok: false; message: string };

/** The zone's planning documents (current versions), adopted first, for the source picker. */
export async function zoneDocumentsAction(zoneId: number): Promise<DocumentsResult> {
  const answer = await adminSend<AdminDocumentList>("GET", "/v1/admin/documents", undefined, { zone_id: zoneId, limit: 200 });
  if (!answer.ok) return { ok: false, message: explain(answer) };
  const rank = (status: string) => (status === "adopted" ? 0 : status === "in_progress" ? 1 : 2);
  const items = answer.data.items
    .map((d) => ({ id: d.id, name: d.name, status: d.status }))
    .sort((a, b) => rank(a.status) - rank(b.status) || a.name.localeCompare(b.name, "en"));
  return { ok: true, items };
}
