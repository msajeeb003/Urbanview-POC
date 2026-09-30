/**
 * The signed-in staff member on the Next server: role and e-mail from `auth()`, and the backend
 * bearer token read straight from the encrypted Auth.js cookie (it is kept out of the session
 * object on purpose, so `/api/auth/session` never hands it to the browser).
 */
import { cookies } from "next/headers";
import { decode } from "next-auth/jwt";

import { auth } from "@/auth";

import { isStaffRole, type StaffRole } from "./sections";

// Auth.js's session cookie: the secure name over https, the plain one on http (development);
// large tokens are split into `.0`, `.1` … chunks.
const COOKIE_NAMES = ["__Secure-authjs.session-token", "authjs.session-token"] as const;

export interface StaffUser {
  email: string | null;
  name: string | null;
  role: StaffRole;
}

export async function currentStaff(): Promise<StaffUser | null> {
  const session = await auth();
  const role = session?.user?.role;
  if (!session || !isStaffRole(role)) return null;
  return { email: session.user.email ?? null, name: session.user.name ?? null, role };
}

function cookieValue(all: { name: string; value: string }[], name: string): string | null {
  const whole = all.find((c) => c.name === name);
  if (whole) return whole.value;
  const chunks = all
    .filter((c) => c.name.startsWith(`${name}.`))
    .map((c) => ({ index: Number(c.name.slice(name.length + 1)), value: c.value }))
    .filter((c) => Number.isInteger(c.index))
    .sort((a, b) => a.index - b.index);
  return chunks.length ? chunks.map((c) => c.value).join("") : null;
}

/** The backend staff session token of this request, or null. */
export async function staffApiToken(): Promise<string | null> {
  const secret = process.env.AUTH_SECRET;
  if (!secret) return null;
  const all = (await cookies()).getAll();
  for (const name of COOKIE_NAMES) {
    const value = cookieValue(all, name);
    if (!value) continue;
    try {
      const payload = await decode({ token: value, secret, salt: name });
      if (payload && typeof payload.apiToken === "string") return payload.apiToken;
    } catch {
      // a stale or foreign cookie: treated as signed out
    }
  }
  return null;
}
