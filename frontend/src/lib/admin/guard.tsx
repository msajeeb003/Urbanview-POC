/**
 * The pages' own role check (the proxy already routed the request; this keeps a page safe on its
 * own): no session -> sign-in, a role outside the section -> the plain "no access" card.
 */
import { redirect } from "next/navigation";
import type { ReactElement } from "react";

import { NoAccess } from "@/components/admin/parts";

import { canOpen, homeFor, isReadOnly, SIGN_IN_PATH, type SectionId } from "./sections";
import { currentStaff, type StaffUser } from "./session";

export type Guard = { staff: StaffUser; readOnly: boolean; denied?: undefined } | { denied: ReactElement };

export async function guard(section: SectionId): Promise<Guard> {
  const staff = await currentStaff();
  if (!staff) redirect(SIGN_IN_PATH);
  if (!canOpen(staff.role, section)) {
    return { denied: <NoAccess home={homeFor(staff.role)} roleLabel={staff.role} /> };
  }
  return { staff, readOnly: isReadOnly(staff.role, section) };
}
