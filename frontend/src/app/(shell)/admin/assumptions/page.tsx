import { AssumptionsScreen } from "@/components/admin/assumptions/assumptions-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { zoneRows } from "@/lib/admin/assumptions";
import { guard } from "@/lib/admin/guard";
import { api } from "@/lib/api/endpoints";
import type { AssumptionSetList, ZoneIndex } from "@/lib/api/types";

// Financial assumptions (wireframe `adminFin`, admins): the figures the feasibility engine reads
// per district, versioned and effective-dated by the API (the panel uses the version that applies
// today; a later date is scheduled). Every version is listed, so the rows can show what is live,
// what is scheduled and the history with its diffs.
export default async function AssumptionsPage() {
  const access = await guard("assumptions");
  if (access.denied) return access.denied;

  let list: AssumptionSetList;
  try {
    list = await adminGet<AssumptionSetList>("/v1/admin/assumptions", { include_history: "true", limit: 500 });
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Financial assumptions" />;
  }
  let zones: ZoneIndex | null = null;
  try {
    zones = await api.zones({ timeoutMs: 3000 });
  } catch {
    zones = null; // the rows still come from the versions themselves
  }
  const rows = zoneRows((zones?.zones ?? []).map((z) => ({ id: z.id, name: z.name })), list.items);
  // a new key after a save (new versions) starts every draft from what is now live
  const key = list.items.map((s) => `${s.id}:${s.status}`).join(",");
  return <AssumptionsScreen key={key} rows={rows} today={list.today} timezone={list.timezone} />;
}
