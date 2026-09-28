import { AdminUnavailable } from "@/components/admin/parts";
import { UsersScreen } from "@/components/admin/users/users-screen";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { guard } from "@/lib/admin/guard";
import type { StaffUserList } from "@/lib/api/types";

// Staff users (admins): add staff, change roles, deactivate (GET / POST / PATCH /v1/admin/users,
// audited by the API). The first admin of a server comes from the command line:
// `python -m core.staff login-link --email <email> --create --role admin`.
export default async function UsersPage() {
  const access = await guard("users");
  if (access.denied) return access.denied;

  let data: StaffUserList;
  try {
    data = await adminGet<StaffUserList>("/v1/admin/users");
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Staff users" />;
  }
  return <UsersScreen users={data.items} me={access.staff.email} />;
}
