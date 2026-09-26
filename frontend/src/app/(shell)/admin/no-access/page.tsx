import { redirect } from "next/navigation";

import { NoAccess } from "@/components/admin/parts";
import { homeFor, SIGN_IN_PATH } from "@/lib/admin/sections";
import { currentStaff } from "@/lib/admin/session";

// Where the proxy sends a role that may not open a section (a rewrite: the address stays).
export default async function NoAccessPage() {
  const staff = await currentStaff();
  if (!staff) redirect(SIGN_IN_PATH);
  return <NoAccess home={homeFor(staff.role)} roleLabel={staff.role} />;
}
