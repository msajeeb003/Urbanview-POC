import { GeometryScreen } from "@/components/admin/review/geometry-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { geometryQuery, parseGeometryFilters } from "@/lib/admin/geometry";
import { guard } from "@/lib/admin/guard";
import type { GeometryPage, ReviewCounters } from "@/lib/api/types";

// Geometry review (the pilot scope's A2 geometry drafts, a sibling of the value queue): staged
// geometry batches with their origin and topology QA, approved or rejected before a publish may
// apply them (admins and reviewers, the review section's roles).
export default async function GeometryReviewPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const access = await guard("review");
  if (access.denied) return access.denied;
  const filters = parseGeometryFilters(await searchParams);

  let page: GeometryPage;
  try {
    page = await adminGet<GeometryPage>("/v1/admin/geometry", geometryQuery(filters));
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Geometry — review queue" />;
  }
  let valuesPending: number | null = null;
  try {
    const counters = await adminGet<ReviewCounters[]>("/v1/admin/review/summary");
    valuesPending = counters.reduce((sum, c) => sum + c.pending, 0);
  } catch {
    valuesPending = null;
  }
  return (
    <GeometryScreen
      key={JSON.stringify(filters)}
      initial={page}
      filters={filters}
      valuesPending={valuesPending}
      me={access.staff.email ?? access.staff.name ?? access.staff.role}
    />
  );
}
