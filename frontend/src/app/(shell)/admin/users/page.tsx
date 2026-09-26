import { AdminCard, AdminUnavailable, DataTable, StatusChip } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { relativeTime } from "@/lib/admin/format";
import { guard } from "@/lib/admin/guard";
import type { StaffUserList } from "@/lib/api/types";

type UserRow = StaffUserList["items"][number];

// Staff users (admins; creating and editing them is the users ticket). The first admin is seeded
// on the server: `python -m core.staff add --email <email> --role admin`.
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
  const now = new Date();

  return (
    <AdminCard title="Staff users" sub="Sign-in by e-mailed link only · roles admin, reviewer, expert">
      <DataTable<UserRow>
        rows={data.items}
        rowKey={(u) => u.id}
        columns={[
          { key: "email", label: "E-mail", mono: true, render: (u) => u.email },
          { key: "name", label: "Name", render: (u) => u.display_name ?? "—" },
          { key: "role", label: "Role", render: (u) => <StatusChip tone="rev">{u.role}</StatusChip> },
          {
            key: "active",
            label: "Status",
            render: (u) => <StatusChip tone={u.is_active ? "ok" : "pend"}>{u.is_active ? "Active" : "Inactive"}</StatusChip>,
          },
          {
            key: "login",
            label: "Last sign-in",
            render: (u) => (u.last_login_at ? relativeTime(u.last_login_at, now) : "never"),
          },
          { key: "sessions", label: "Sessions", mono: true, render: (u) => u.open_sessions },
        ]}
      />
    </AdminCard>
  );
}
