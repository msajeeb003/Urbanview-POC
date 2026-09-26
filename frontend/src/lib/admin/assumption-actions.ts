"use server";

/**
 * Server actions of the Financial assumptions tab (admins). "Save changes" sends every changed
 * zone as a new version applying from one date in one request (`POST
 * /v1/admin/assumptions/batch`: all or nothing, one audit row per version with the previous and
 * the new figures); the preview asks the API to compute a parcel's Group 2 with the unsaved
 * figures (`POST /v1/admin/assumptions/preview`: nothing is written). A refusal comes back as
 * `{ok: false, message}` in plain words, naming the zone when the API points at one.
 */
import { revalidatePath } from "next/cache";

import type {
  AssumptionSetIn,
  AssumptionsBatchOut,
  AssumptionsPreview,
  AssumptionsPreviewIn,
  PreviewParcel,
  PreviewParcelList,
} from "@/lib/api/types";

import { adminSend } from "./api";
import { dayLabel } from "./assumptions";
import { currentStaff } from "./session";

async function admin(): Promise<string | null> {
  const staff = await currentStaff();
  return staff?.role === "admin" ? null : "Only admins change the financial assumptions.";
}

interface Problem {
  status: number;
  message?: string;
  details?: unknown;
}

function explain(problem: Problem, zoneNames: string[] = []): string {
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status === 0) return "The data service did not answer. Try again in a moment.";
  if (problem.status >= 500) return "The data service had a problem. Try again in a moment.";
  if (problem.status === 422 && Array.isArray(problem.details)) {
    const lines = (problem.details as { loc?: (string | number)[]; msg?: string }[])
      .map((d) => {
        const msg = d.msg?.replace(/^Value error, /, "");
        const index = d.loc?.[0] === "body" && d.loc?.[1] === "sets" ? Number(d.loc[2]) : null;
        const zone = index != null && Number.isInteger(index) ? zoneNames[index] : null;
        return msg ? (zone ? `${zone}: ${msg}` : msg) : null;
      })
      .filter(Boolean);
    if (lines.length) return lines.join("; ");
  }
  return problem.message || "The request was refused.";
}

export type SaveResult = { ok: true; message: string } | { ok: false; message: string };

export interface ZoneSet {
  zoneName: string;
  set: AssumptionSetIn;
}

/** One new version per changed zone, all applying from `effectiveFrom`. */
export async function saveAssumptionsAction(effectiveFrom: string, sets: ZoneSet[]): Promise<SaveResult> {
  const denied = await admin();
  if (denied) return { ok: false, message: denied };
  if (!sets.length) return { ok: false, message: "Nothing changed." };
  const answer = await adminSend<AssumptionsBatchOut>("POST", "/v1/admin/assumptions/batch", {
    effective_from: effectiveFrom,
    sets: sets.map((s) => s.set),
  });
  if (!answer.ok) return { ok: false, message: explain(answer, sets.map((s) => s.zoneName)) };
  revalidatePath("/admin/assumptions");
  const items = answer.data.items;
  const scheduled = items.filter((i) => i.status === "scheduled");
  const names = items.map((i) => `${i.zone_name ?? "zone"} v${i.version}`).join(", ");
  const when = scheduled.length ? `scheduled from ${dayLabel(items[0]?.applies_from)}` : "live now";
  return { ok: true, message: `Saved ${names} — ${when}` };
}

export type ParcelsResult = { ok: true; items: PreviewParcel[] } | { ok: false; message: string };

/** Covered parcels of the zone to try the draft on. */
export async function previewParcelsAction(zoneId: number): Promise<ParcelsResult> {
  const denied = await admin();
  if (denied) return { ok: false, message: denied };
  const answer = await adminSend<PreviewParcelList>("GET", "/v1/admin/assumptions/preview-parcels", undefined, {
    zone_id: zoneId,
  });
  if (!answer.ok) return { ok: false, message: explain(answer) };
  return { ok: true, items: answer.data.items };
}

export type PreviewResult = { ok: true; preview: AssumptionsPreview } | { ok: false; message: string };

/** A parcel's Group 2 today and with the draft (nothing is written). */
export async function previewAction(input: AssumptionsPreviewIn): Promise<PreviewResult> {
  const denied = await admin();
  if (denied) return { ok: false, message: denied };
  const answer = await adminSend<AssumptionsPreview>("POST", "/v1/admin/assumptions/preview", input);
  if (!answer.ok) {
    if (answer.status === 404) return { ok: false, message: "That parcel is not in the database any more." };
    return { ok: false, message: explain(answer) };
  }
  return { ok: true, preview: answer.data };
}
