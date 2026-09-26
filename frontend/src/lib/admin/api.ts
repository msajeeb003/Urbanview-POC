/**
 * Admin data on the Next server: every call carries the signed-in staff member's bearer token, so
 * the API applies its own role checks (403, never 404) to what the console asks for.
 *
 * - no session / an ended session (401) -> the sign-in page;
 * - a refused role (403) -> `AdminAccessDenied`, which the page renders as "no access";
 * - anything else is thrown for the page's error state.
 */
import { redirect } from "next/navigation";

import { ApiError, apiRequest, type Query } from "@/lib/api/client";

import { SIGN_IN_PATH } from "./sections";
import { staffApiToken } from "./session";
import { bearer } from "./staff-api";

export class AdminAccessDenied extends Error {
  constructor(message = "This section is not open to your role") {
    super(message);
    this.name = "AdminAccessDenied";
  }
}

export async function adminGet<T>(path: string, query?: Query): Promise<T> {
  const token = await staffApiToken();
  if (!token) redirect(SIGN_IN_PATH);
  try {
    const { data } = await apiRequest<T>(path, {
      query,
      headers: bearer(token),
      timeoutMs: 10_000,
      retry429: 1,
    });
    return data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) redirect(`${SIGN_IN_PATH}?expired=1`);
    if (err instanceof ApiError && err.status === 403) throw new AdminAccessDenied();
    throw err;
  }
}

/** What a staff write answered: the data, or the refusal (status, code, message, details). */
export type AdminWrite<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; code: string; message: string; details?: unknown };

/**
 * A staff write (`POST` / `PATCH` / `DELETE`) with the signed-in member's bearer token. Refusals
 * come back as values for the server action to explain (no throw); an ended session still goes to
 * the sign-in page.
 */
export async function adminSend<T>(
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
  query?: Query,
): Promise<AdminWrite<T>> {
  const token = await staffApiToken();
  if (!token) redirect(SIGN_IN_PATH);
  try {
    const { data, status } = await apiRequest<T>(path, {
      method,
      body,
      query,
      headers: bearer(token),
      timeoutMs: 30_000,
      retry429: 1,
    });
    return { ok: true, status, data };
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 401) redirect(`${SIGN_IN_PATH}?expired=1`);
      return { ok: false, status: err.status, code: err.code, message: err.message, details: err.details };
    }
    throw err;
  }
}
