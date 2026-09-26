import { EngineScreen } from "@/components/admin/engine/engine-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { inputRows } from "@/lib/admin/engine";
import { guard } from "@/lib/admin/guard";
import { api } from "@/lib/api/endpoints";
import type { EngineProposalList, MunicipalityProfile } from "@/lib/api/types";

// Calculation engine (wireframe `adminEngine`, admins): the client-owned formulas the shared
// engine runs, the datasets they draw on (named from the municipality profile's sources), the
// proposals staff recorded for the client's review, and the engine's version and changelog.
export default async function EnginePage() {
  const access = await guard("engine");
  if (access.denied) return access.denied;

  let proposals: EngineProposalList;
  try {
    proposals = await adminGet<EngineProposalList>("/v1/admin/engine/proposals");
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Calculation engine" />;
  }
  let profile: MunicipalityProfile | null = null;
  try {
    profile = await api.municipality({ timeoutMs: 3000 });
  } catch {
    profile = null; // the dataset rows then name no sources
  }
  return <EngineScreen proposals={proposals.items} inputs={inputRows(profile?.sources)} />;
}
