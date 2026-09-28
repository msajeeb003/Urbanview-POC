/**
 * The AI extraction page's rules (`/admin/ai`, admins): the key's format check with the API's
 * exact messages (`core.app_secrets.anthropic_key_problem`, so the console refuses a malformed key
 * before it leaves the browser and says the same thing the API would), the readiness chips, the
 * words for a connection test's outcome and the page's number formats. Pure; tested in
 * `ai.test.ts`.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { AiCheck, AiCheckStatus, AiChecklistItem, AiKeySource } from "@/lib/api/types";

export const KEY_PREFIX = "sk-ant-";
const ADMIN_PREFIX = "sk-ant-admin";
const MIN_LENGTH = 40;
const MAX_LENGTH = 256;

export const KEY_MESSAGES = {
  empty: "Paste the API key.",
  whitespace: "The key must not contain spaces or line breaks.",
  prefix: "An Anthropic API key starts with sk-ant-.",
  admin: "This is an Admin API key: paste a regular API key.",
  short: "The key is too short to be an Anthropic API key.",
  long: "The key is too long to be an Anthropic API key.",
  characters: "The key contains characters an Anthropic API key does not use.",
} as const;

/** The first format rule the (trimmed) value breaks, in the API's order, or null. */
export function keyProblem(raw: string): string | null {
  const value = raw.trim();
  if (!value) return KEY_MESSAGES.empty;
  if (/\s/.test(value)) return KEY_MESSAGES.whitespace;
  if (!value.startsWith(KEY_PREFIX)) return KEY_MESSAGES.prefix;
  if (value.startsWith(ADMIN_PREFIX)) return KEY_MESSAGES.admin;
  if (value.length < MIN_LENGTH) return KEY_MESSAGES.short;
  if (value.length > MAX_LENGTH) return KEY_MESSAGES.long;
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return KEY_MESSAGES.characters;
  return null;
}

/** `sk-ant-…1234`: the only form of a key the page ever shows. */
export function maskedKey(last4: string | null | undefined): string {
  return last4 ? `${KEY_PREFIX}…${last4}` : "none";
}

export const SOURCE_LABELS: Record<AiKeySource, string> = {
  server_env: "server environment (ANTHROPIC_API_KEY)",
  console: "saved in this console",
  none: "no key",
};

export const CHECK_LABELS: Record<AiCheckStatus, string> = {
  ok: "Connected",
  no_key: "No key",
  invalid_key: "Invalid key",
  permission_denied: "Permission denied",
  no_credit: "No credit",
  model_unavailable: "Model unavailable",
  rate_limited: "Rate limited",
  overloaded: "API overloaded",
  network_error: "Network error",
  error: "Error",
};

const ACTIVE = new Set(["queued", "running", "retrying"]);

/** A connection test is queued or running (the page then re-reads itself every 2 s). */
export function testInFlight(check: AiCheck | null | undefined): boolean {
  return check != null && ACTIVE.has(check.job.status);
}

/** The chip of the last test: green when it connected, amber while it runs, red otherwise. */
export function checkChip(check: AiCheck | null | undefined): { tone: ChipTone; label: string } {
  if (check == null) return { tone: "pend", label: "Never tested" };
  if (testInFlight(check)) return { tone: "rev", label: check.job.status === "queued" ? "Queued" : "Testing" };
  const status = check.status ?? "error";
  return { tone: status === "ok" ? "ok" : "pend", label: CHECK_LABELS[status] ?? status };
}

/** The toast when a test finishes: the outcome, then what the API says to do about it. */
export function checkToast(check: AiCheck): string {
  const status = check.status ?? "error";
  const head = `Connection test: ${CHECK_LABELS[status] ?? status}.`;
  if (status === "ok") return head;
  const detail = check.detail_en ?? check.job.error;
  return detail ? `${head} ${detail}` : head;
}

/** The readiness chips: Ready / Missing / Unknown (the API could not tell, or a test runs). */
export function checklistChip(item: AiChecklistItem): { tone: ChipTone; label: string } {
  if (item.ok === true) return { tone: "ok", label: "Ready" };
  if (item.ok === false) return { tone: item.required ? "pend" : "rev", label: "Missing" };
  return { tone: "rev", label: "Unknown" };
}

/** The required items still missing, for the "Not ready" head. */
export function missingRequired(items: readonly AiChecklistItem[]): AiChecklistItem[] {
  return items.filter((i) => i.required && i.ok !== true);
}

/** `€0.0123` for a job's spend, `€12.34` once it is real money. */
export function eur(value: number | null | undefined): string {
  if (value == null) return "—";
  const digits = Math.abs(value) >= 1 ? 2 : 4;
  return `€${value.toFixed(digits)}`;
}

/** `1 234 567` tokens. */
export function tokens(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

/** `1 200 ms`, `2.4 s`. */
export function latency(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** Seconds a queued test has waited: over 30 s the page hints that no worker may be listening. */
export function waitedSeconds(check: AiCheck | null | undefined, now: Date): number {
  if (!testInFlight(check) || check == null) return 0;
  const since = Date.parse(check.job.started_at ?? check.job.requested_at);
  if (Number.isNaN(since)) return 0;
  return Math.max(0, Math.floor((now.getTime() - since) / 1000));
}

export const SLOW_TEST_SECONDS = 30;
