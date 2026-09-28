"use server";

/**
 * Server actions of the AI extraction page (admins): save the Anthropic API key (`PUT
 * /v1/admin/ai/key`, encrypted on the server, write-only), remove it (`DELETE`), and run a
 * connection test (`POST /v1/admin/ai/check`, a job the worker runs).
 *
 * The key travels only in this action's argument and the API's request body: it is never logged,
 * put in a URL, echoed in a message or kept anywhere on the Next server. Every answer is
 * `{ok, message}` like the other admin screens; the page re-reads `GET /v1/admin/ai` after a
 * write (`revalidatePath`).
 */
import { revalidatePath } from "next/cache";

import type { AdminJob, AiStatus } from "@/lib/api/types";

import { adminSend, type AdminWrite } from "./api";
import { keyProblem, maskedKey } from "./ai";
import { currentStaff } from "./session";

export type AiActionResult = { ok: boolean; message: string };

const PAGE = "/admin/ai";
const NOT_ADMIN = "Only admins change AI extraction settings.";
const NO_ANSWER = "The data service did not answer. Try again in a moment.";

async function adminOnly(): Promise<AiActionResult | null> {
  const staff = await currentStaff();
  return staff?.role === "admin" ? null : { ok: false, message: NOT_ADMIN };
}

/** The API's refusal as one sentence (validation details never carry the key: `x-redact-input`). */
function refusal<T>(answer: Extract<AdminWrite<T>, { ok: false }>, fallback: string): string {
  if (answer.status === 403) return "Your role cannot do this.";
  if (answer.status === 0 || answer.status >= 500) return answer.message || NO_ANSWER;
  const details = Array.isArray(answer.details) ? (answer.details as { msg?: string }[]) : [];
  const msgs = details.map((d) => d.msg?.replace(/^Value error, /, "")).filter(Boolean);
  return msgs.join("; ") || answer.message || fallback;
}

export async function saveKeyAction(apiKey: string): Promise<AiActionResult> {
  const denied = await adminOnly();
  if (denied) return denied;
  const value = typeof apiKey === "string" ? apiKey.trim() : "";
  const problem = keyProblem(value);
  if (problem) return { ok: false, message: problem };
  const answer = await adminSend<AiStatus>("PUT", "/v1/admin/ai/key", { api_key: value });
  if (!answer.ok) {
    if (answer.status === 409) {
      return { ok: false, message: answer.message || "The server cannot save keys (SECRETS_ENCRYPTION_KEY is not set)." };
    }
    return { ok: false, message: refusal(answer, "The key was refused.") };
  }
  const key = answer.data.key;
  const testing = answer.data.check?.job.status === "queued" || answer.data.check?.job.status === "running";
  revalidatePath(PAGE);
  const saved = `Key saved (${maskedKey(key.console_key_last4)}).`;
  if (!key.console_key_active) return { ok: true, message: `${saved} ${key.console_note_en ?? ""}`.trim() };
  return { ok: true, message: testing ? `${saved} A connection test is running.` : `${saved} The connection test could not be queued.` };
}

export async function removeKeyAction(): Promise<AiActionResult> {
  const denied = await adminOnly();
  if (denied) return denied;
  const answer = await adminSend<AiStatus>("DELETE", "/v1/admin/ai/key");
  if (!answer.ok) {
    if (answer.status === 404) return { ok: false, message: "No key is saved in this console." };
    return { ok: false, message: refusal(answer, "The key could not be removed.") };
  }
  revalidatePath(PAGE);
  const key = answer.data.key;
  return {
    ok: true,
    message: key.configured
      ? `Saved key removed. The server environment's key (${maskedKey(key.last4)}) stays in use.`
      : "Saved key removed. AI extraction has no key until one is set.",
  };
}

export async function testConnectionAction(): Promise<AiActionResult> {
  const denied = await adminOnly();
  if (denied) return denied;
  const answer = await adminSend<AdminJob>("POST", "/v1/admin/ai/check");
  if (!answer.ok) {
    if (answer.status === 503 && answer.code === "service_unavailable" && answer.details) {
      revalidatePath(PAGE);
      return { ok: false, message: "The job queue is unavailable: the test was recorded as failed." };
    }
    return { ok: false, message: refusal(answer, "The test could not be started.") };
  }
  revalidatePath(PAGE);
  return { ok: true, message: answer.status === 200 ? "A connection test is already running." : "Connection test started." };
}
