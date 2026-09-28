import { ReviewScreen } from "@/components/admin/review/review-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { zoneOptions } from "@/lib/admin/data";
import { guard } from "@/lib/admin/guard";
import { parseReviewFilters, reviewQuery } from "@/lib/admin/review";
import { canOpen } from "@/lib/admin/sections";
import { api } from "@/lib/api/endpoints";
import type { PublishStatus, ReviewCounters, ReviewPage, ZoneIndex } from "@/lib/api/types";

async function orNull<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch {
    return null;
  }
}

// AI review queue (wireframe `adminReview`): every extracted value is approved, corrected or
// rejected against its cited PDF page before it can publish (admins and reviewers).
export default async function ReviewQueuePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const access = await guard("review");
  if (access.denied) return access.denied;
  const filters = parseReviewFilters(await searchParams);

  let page: ReviewPage;
  let counters: ReviewCounters[];
  try {
    [page, counters] = await Promise.all([
      adminGet<ReviewPage>("/v1/admin/review", reviewQuery(filters)),
      adminGet<ReviewCounters[]>("/v1/admin/review/summary"),
    ]);
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="AI extraction — review queue" />;
  }
  const [publish, zones] = await Promise.all([
    orNull(() => adminGet<PublishStatus>("/v1/admin/publish")),
    orNull<ZoneIndex>(() => api.zones({ timeoutMs: 3000 })),
  ]);
  return (
    <ReviewScreen
      key={JSON.stringify(filters)}
      initial={page}
      filters={filters}
      counters={counters}
      zones={zoneOptions(zones?.zones)}
      publish={publish}
      me={access.staff.email ?? access.staff.name ?? access.staff.role}
      canPublish={canOpen(access.staff.role, "publish")}
    />
  );
}
