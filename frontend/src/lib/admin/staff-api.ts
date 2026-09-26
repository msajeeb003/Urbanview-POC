/**
 * The backend calls behind staff sign-in (server only: Auth.js callbacks and server actions).
 *
 * The backend owns the magic link: `POST /v1/auth/magic-link` queues its `magic_link` e-mail
 * (single use, `MAGIC_LINK_EXPIRES_SECONDS`, same answer for unknown addresses), the link opens
 * `/admin/login?token=…`, and `POST /v1/auth/magic-link/exchange` turns the token into a staff
 * session bearer token. The role always comes from `GET /v1/admin/users/me`.
 */
import { ApiError, apiRequest } from "@/lib/api/client";
import type { StaffMe, StaffSession } from "@/lib/api/types";

import { isStaffRole, type StaffRole } from "./sections";

const TIMEOUT_MS = 8000;

export function bearer(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/** Queue the login e-mail. Resolves for every well-formed address (no enumeration). */
export async function requestMagicLink(email: string): Promise<"sent" | "invalid_email"> {
  try {
    await apiRequest("/v1/auth/magic-link", {
      method: "POST",
      body: { email },
      timeoutMs: TIMEOUT_MS,
      retry429: 0,
    });
    return "sent";
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) return "invalid_email";
    throw err;
  }
}

/** The login link's token -> a staff session, or null for an invalid / used / expired link. */
export async function exchangeMagicLink(token: string): Promise<StaffSession | null> {
  try {
    const { data } = await apiRequest<StaffSession>("/v1/auth/magic-link/exchange", {
      method: "POST",
      body: { token },
      timeoutMs: TIMEOUT_MS,
      retry429: 0,
    });
    return data;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 422)) return null;
    throw err;
  }
}

export type MeResult = { kind: "ok"; me: StaffMe; role: StaffRole } | { kind: "unauthorized" } | { kind: "unavailable" };

/** Who the bearer is. `unauthorized` = the session ended (revoked, expired, user deactivated). */
export async function fetchMe(apiToken: string): Promise<MeResult> {
  try {
    const { data } = await apiRequest<StaffMe>("/v1/admin/users/me", {
      headers: bearer(apiToken),
      timeoutMs: TIMEOUT_MS,
      retry429: 0,
    });
    if (!isStaffRole(data.role)) return { kind: "unauthorized" };
    return { kind: "ok", me: data, role: data.role };
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return { kind: "unauthorized" };
    return { kind: "unavailable" };
  }
}

/** End the backend session (best effort: the Auth.js cookie is cleared either way). */
export async function revokeSession(apiToken: string): Promise<void> {
  try {
    await apiRequest("/v1/auth/sign-out", {
      method: "POST",
      headers: bearer(apiToken),
      timeoutMs: TIMEOUT_MS,
      retry429: 0,
    });
  } catch {
    // the session expires on its own; signing out of the console must not fail on this
  }
}
