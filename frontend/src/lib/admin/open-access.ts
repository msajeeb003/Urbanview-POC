/**
 * Open access (temporary, off by default): the admin console without the magic-link sign-in, for
 * as long as the server cannot mail the links (no SMTP details yet).
 *
 * With `ADMIN_OPEN_ACCESS_TOKEN` set (server only, never NEXT_PUBLIC: an admin entry of the API's
 * `ADMIN_API_TOKENS`), every visitor of /admin is the admin "Open access": the proxy, the pages and
 * the server actions let them in (`currentStaff`) and every console call to the API carries that
 * token (`consoleApiToken`), so the audit log names the token's subject as the actor. Unset, the
 * sign-in works as before; none of it is removed.
 */
import type { StaffUser } from "./session";

export function openAccessToken(): string | null {
  return process.env.ADMIN_OPEN_ACCESS_TOKEN?.trim() || null;
}

export const OPEN_ACCESS_STAFF: StaffUser = {
  email: null,
  name: "Open access",
  role: "admin",
  openAccess: true,
};
