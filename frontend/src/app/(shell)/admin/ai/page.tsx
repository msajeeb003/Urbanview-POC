import { AiScreen } from "@/components/admin/ai/ai-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { guard } from "@/lib/admin/guard";
import type { AiStatus } from "@/lib/api/types";

// AI extraction settings (admins; not in the mock): readiness, the Anthropic API key (write-only,
// encrypted on the server), a connection test the worker runs, the model settings and the spend so
// far. Everything shown comes from `GET /v1/admin/ai`; the writes are in `lib/admin/ai-actions.ts`.
export default async function AiPage() {
  const access = await guard("ai");
  if (access.denied) return access.denied;

  let status: AiStatus;
  try {
    status = await adminGet<AiStatus>("/v1/admin/ai");
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="AI extraction" />;
  }
  return <AiScreen status={status} />;
}
