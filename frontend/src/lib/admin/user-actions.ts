"use server";

/**
 * Server actions of the Users page (admins): add a staff member (`POST /v1/admin/users`), change a
 * role, a name or the active flag (`PATCH /v1/admin/users/{id}`). The API audits every write
 * (`user.create`, `user.update`), revokes a deactivated member's sessions and refuses changes to
 * one's own role or status; staff sign in by e-mailed link, so nothing here sets a password.
 */
import { revalidatePath } from "next/cache";

import type { StaffUserList } from "@/lib/api/types";

import { adminSend } from "./api";
import type { StaffRole } from "./sections";
import { currentStaff } from "./session";
import { checkNewUser, explainUserProblem, type NewUser } from "./users";

type StaffUserOut = StaffUserList["items"][number];

export type UserResult = { ok: true; message: string } | { ok: false; message: string };

async function denied(): Promise<string | null> {
  const staff = await currentStaff();
  return staff?.role === "admin" ? null : "Only administrators manage staff.";
}

export async function addUserAction(user: NewUser): Promise<UserResult> {
  const refusal = (await denied()) ?? checkNewUser(user);
  if (refusal) return { ok: false, message: refusal };
  const result = await adminSend<StaffUserOut>("POST", "/v1/admin/users", {
    email: user.email.trim(),
    role: user.role,
    display_name: user.displayName.trim() || null,
  });
  if (!result.ok) return { ok: false, message: explainUserProblem(result) };
  revalidatePath("/admin/users");
  return { ok: true, message: `${result.data.email} can now sign in as ${result.data.role}` };
}

export async function updateUserAction(
  userId: number,
  patch: { role?: StaffRole; is_active?: boolean; display_name?: string | null },
): Promise<UserResult> {
  const refusal = await denied();
  if (refusal) return { ok: false, message: refusal };
  const result = await adminSend<StaffUserOut>("PATCH", `/v1/admin/users/${userId}`, patch);
  if (!result.ok) return { ok: false, message: explainUserProblem(result) };
  revalidatePath("/admin/users");
  const u = result.data;
  const message =
    patch.is_active === false
      ? `${u.email} is deactivated; their sessions are closed`
      : patch.is_active === true
        ? `${u.email} is active again`
        : patch.role
          ? `${u.email} is now ${u.role}`
          : `${u.email} is updated`;
  return { ok: true, message };
}
