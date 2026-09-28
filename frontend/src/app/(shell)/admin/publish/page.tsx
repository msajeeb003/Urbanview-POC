import { AdminUnavailable } from "@/components/admin/parts";
import { PublishScreen } from "@/components/admin/publish/publish-screen";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { guard } from "@/lib/admin/guard";
import type { PublishStatus } from "@/lib/api/types";

// Publish (admins and reviewers; the pilot scope's A4): the data version the map serves, the
// publish button with its blockers and progress, and the kept versions with rollback, from
// GET /v1/admin/publish.
export default async function PublishPage() {
  const access = await guard("publish");
  if (access.denied) return access.denied;

  let status: PublishStatus;
  try {
    status = await adminGet<PublishStatus>("/v1/admin/publish");
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Publish" />;
  }
  return <PublishScreen initial={status} />;
}
