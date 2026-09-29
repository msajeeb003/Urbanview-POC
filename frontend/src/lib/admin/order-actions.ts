"use server";

/**
 * Server actions of the Orders tab. Every call goes to the staff orders API with the signed-in
 * member's bearer token; the API audits each change (before / after, the note) and enforces the
 * status flow and the roles (experts: their own orders, report upload only). A refusal comes back
 * as `{ok: false, message}` in plain words; a success re-reads the page (list + open order).
 *
 * The report upload is not an action (server actions cap request bodies): the browser sends the
 * PDF to `/api/admin/orders/[id]/report`, which streams it to `POST /v1/admin/orders/{id}/report`.
 */
import { revalidatePath } from "next/cache";

import type { OrderDetail } from "@/lib/api/types";

import { adminSend } from "./api";
import { isManager } from "./orders";
import { currentStaff } from "./session";

export type OrderResult = { ok: true; message: string } | { ok: false; message: string };

async function manager(): Promise<string | null> {
  const staff = await currentStaff();
  return staff && isManager(staff.role) ? null : "Only admins manage orders.";
}

function explain(problem: { status: number; message?: string; details?: unknown }): string {
  if (problem.status === 403) return "Your role cannot do this.";
  if (problem.status === 0) return "The data service did not answer. Try again in a moment.";
  if (problem.status >= 500) return "The data service had a problem. Try again in a moment.";
  if (problem.status === 422 && Array.isArray(problem.details)) {
    const msgs = (problem.details as { msg?: string }[]).map((d) => d.msg?.replace(/^Value error, /, "")).filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return problem.message || "The request was refused.";
}

function done(message: string): OrderResult {
  revalidatePath("/admin/orders");
  return { ok: true, message };
}

export interface PaymentInput {
  kind: "received" | "not_received" | "refunded";
  amount: number | null;
  date: string;
  reference: string;
  note: string;
}

/** `POST /v1/admin/orders/{id}/payment`: received (→ paid), not received (a note), refunded. */
export async function paymentAction(orderId: number, input: PaymentInput): Promise<OrderResult> {
  const denied = await manager();
  if (denied) return { ok: false, message: denied };
  if (input.kind !== "not_received") {
    if (input.amount == null || !(input.amount > 0)) return { ok: false, message: "Enter the amount." };
    if (!input.date) return { ok: false, message: "Enter the date of the transfer." };
    if (!input.reference.trim()) return { ok: false, message: "Enter the bank reference." };
  } else if (!input.note.trim()) {
    return { ok: false, message: "Say what was checked, e.g. “nothing on the statement of 26 Sep”." };
  }
  const result = await adminSend<OrderDetail>("POST", `/v1/admin/orders/${orderId}/payment`, {
    status: input.kind,
    amount_eur: input.kind === "not_received" ? null : input.amount,
    received_on: input.kind === "not_received" ? null : input.date,
    reference: input.kind === "not_received" ? null : input.reference.trim(),
    note: input.note.trim() || null,
  });
  if (!result.ok) return { ok: false, message: explain(result) };
  return done(
    input.kind === "received" ? "Payment recorded — the order is paid" : input.kind === "refunded" ? "Refund recorded" : "Payment check recorded",
  );
}

/** `POST /v1/admin/orders/{id}/assign`: a paid order moves to in progress (reassign later). */
export async function assignAction(orderId: number, expertUserId: number, note: string): Promise<OrderResult> {
  const denied = await manager();
  if (denied) return { ok: false, message: denied };
  const result = await adminSend<OrderDetail>("POST", `/v1/admin/orders/${orderId}/assign`, {
    expert_user_id: expertUserId,
    note: note.trim() || null,
  });
  if (!result.ok) return { ok: false, message: explain(result) };
  const who = result.data.assignee?.display_name ?? result.data.assignee?.email ?? "the expert";
  return done(result.data.status === "in_progress" ? `Assigned to ${who} — work started` : `Assigned to ${who}`);
}
