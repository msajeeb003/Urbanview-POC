"use server";

/**
 * Server actions of the geometry review (`/admin/review/geometry`). Every call goes to the staff
 * API with the signed-in member's bearer token (the API audits each decision: actor, before /
 * after, the note). A decision answers the updated draft and the queue's counts; refusals come
 * back as `{ok: false, message}` in plain words. Admins and reviewers decide (the review
 * section's roles), as the API checks again.
 */
import { unstable_rethrow } from "next/navigation";

import type { GeometryCounts, GeometryDraft, GeometryFeatures, GeometryPage } from "@/lib/api/types";

import { adminGet, adminSend } from "./api";
import { explainGeometryProblem } from "./geometry";
import { canOpen } from "./sections";
import { currentStaff } from "./session";

export type Result<T> = { ok: true; message: string; data: T } | { ok: false; message: string };

export interface GeometryDecision {
  draft: GeometryDraft;
  counts: GeometryCounts | null;
}

async function reviewer(): Promise<string | null> {
  const staff = await currentStaff();
  return staff && canOpen(staff.role, "review") ? null : "Your role cannot review geometry.";
}

async function counts(): Promise<GeometryCounts | null> {
  try {
    return (await adminGet<GeometryPage>("/v1/admin/geometry", { limit: 1 })).counts;
  } catch (err) {
    unstable_rethrow(err);
    return null;
  }
}

async function decide(path: string, body: unknown, message: string): Promise<Result<GeometryDecision>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<GeometryDraft>("POST", path, body);
  if (!result.ok) return { ok: false, message: explainGeometryProblem(result) };
  return { ok: true, message, data: { draft: result.data, counts: await counts() } };
}

/** `POST /v1/admin/geometry/{id}/approve`: the batch goes out with the next publish. */
export async function approveGeometryAction(batchId: number, note?: string): Promise<Result<GeometryDecision>> {
  return decide(`/v1/admin/geometry/${batchId}/approve`, note?.trim() ? { note: note.trim() } : {}, "Geometry approved");
}

/** `POST /v1/admin/geometry/{id}/reject`: never published; the reason is required. */
export async function rejectGeometryAction(batchId: number, note: string): Promise<Result<GeometryDecision>> {
  if (!note.trim()) return { ok: false, message: "Give the reason for rejecting." };
  return decide(`/v1/admin/geometry/${batchId}/reject`, { note: note.trim() }, "Geometry rejected");
}

/** The batch's features for the preview (simplified GeoJSON, issue codes per feature, gaps). */
export async function geometryFeaturesAction(batchId: number): Promise<Result<GeometryFeatures>> {
  const denied = await reviewer();
  if (denied) return { ok: false, message: denied };
  try {
    return { ok: true, message: "", data: await adminGet<GeometryFeatures>(`/v1/admin/geometry/${batchId}/features`) };
  } catch (err) {
    unstable_rethrow(err);
    return { ok: false, message: "The features could not be loaded." };
  }
}
