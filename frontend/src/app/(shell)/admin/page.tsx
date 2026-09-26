import { redirect } from "next/navigation";

import { homeFor, SIGN_IN_PATH } from "@/lib/admin/sections";
import { currentStaff } from "@/lib/admin/session";

// /admin -> the role's first tab (the proxy does the same before the page renders).
export default async function AdminHome() {
  const staff = await currentStaff();
  redirect(staff ? homeFor(staff.role) : SIGN_IN_PATH);
}
