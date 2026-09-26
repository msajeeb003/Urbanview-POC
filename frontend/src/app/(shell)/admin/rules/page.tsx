import { AdminUnavailable } from "@/components/admin/parts";
import { RulesScreen } from "@/components/admin/rules/rules-screen";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { guard } from "@/lib/admin/guard";
import { localToday } from "@/lib/admin/rules";
import { api } from "@/lib/api/endpoints";
import type { MunicipalityProfile, ZoneIndex, ZoneParameterList } from "@/lib/api/types";

async function orNull<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch {
    return null;
  }
}

// Planning rules (wireframe `adminRules`): each zone's typical planning values — its current zone
// parameter set with the source document, page and verification. Admins add and edit rules (each
// save a new version, audited); reviewers read them.
export default async function RulesPage() {
  const access = await guard("rules");
  if (access.denied) return access.denied;

  let list: ZoneParameterList;
  try {
    list = await adminGet<ZoneParameterList>("/v1/admin/zone-parameters", { limit: 500 });
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Planning rules" />;
  }
  const [zones, profile] = await Promise.all([
    orNull<ZoneIndex>(() => api.zones({ timeoutMs: 3000 })),
    orNull<MunicipalityProfile>(() => api.municipality({ timeoutMs: 3000 })),
  ]);
  const rules = [...list.items].sort((a, b) => (a.zone_name ?? "").localeCompare(b.zone_name ?? "", "en"));
  const zoneOptions = (zones?.zones ?? []).map((z) => ({ id: z.id, name: z.name })).sort((a, b) => a.name.localeCompare(b.name, "en"));
  return (
    <RulesScreen
      key={rules.map((r) => r.id).join(",")}
      rules={rules}
      zones={zoneOptions}
      readOnly={access.readOnly}
      today={localToday(profile?.timezone)}
      me={access.staff.name ?? access.staff.email ?? "staff"}
    />
  );
}
