/**
 * The rules of the Users page (`/admin/users`, admins): the three roles in the pilot scope's words,
 * the add-staff form's checks (the API's: a well-formed e-mail, a role), which rows the signed-in
 * admin may change (not their own role or status: the API refuses with 409), and what a refused
 * write means in one sentence. Staff sign in by e-mailed link only: there is no password to set.
 */
import type { StaffRole } from "./sections";

export const ROLES: readonly { value: StaffRole; label: string; hint: string }[] = [
  { value: "admin", label: "Admin", hint: "Everything: documents, assumptions, orders, publishing, users" },
  { value: "reviewer", label: "Reviewer", hint: "Approves extracted values and publishes; reads documents" },
  { value: "expert", label: "Expert", hint: "Produces the paid reports: the orders assigned to them" },
];

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export interface NewUser {
  email: string;
  displayName: string;
  role: StaffRole | "";
}

/** The form's problem in one sentence, or null when it may be sent. */
export function checkNewUser(user: NewUser): string | null {
  if (!EMAIL_RE.test(user.email.trim())) return "Enter the person's work e-mail.";
  if (!user.role) return "Choose a role.";
  if (user.displayName.trim().length > 200) return "The name is too long.";
  return null;
}

/** The signed-in admin's own row (matched by e-mail; a service token has none). */
export function isSelf(email: string, me: string | null | undefined): boolean {
  return !!me && email.trim().toLowerCase() === me.trim().toLowerCase();
}

/** What a refused `POST` / `PATCH /v1/admin/users` means for the person using the page. */
export function explainUserProblem(problem: { status: number; message?: string; details?: unknown }): string {
  if (problem.status === 403) return "Only administrators manage staff.";
  if (problem.status === 409) {
    const reason = (problem.details as { reason?: string } | undefined)?.reason;
    return reason === "self"
      ? "You cannot change your own role or deactivate yourself."
      : "A staff member with this e-mail already exists.";
  }
  if (problem.status === 404) return "This staff member no longer exists: reload the page.";
  if (problem.status === 0 || problem.status >= 500) return "The staff service did not answer. Try again in a moment.";
  if (problem.status === 422) return "Check the e-mail and the role.";
  return problem.message || "The change was refused.";
}
