import { AdminCard, AdminUnavailable, DataTable, StatCard, StatusChip } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { extractionLabel, liveChip, reviewLabel } from "@/lib/admin/format";
import { guard } from "@/lib/admin/guard";
import type { AdminOverview, DistrictStatus } from "@/lib/api/types";
import { formatEur } from "@/lib/format";

// Overview (wireframe `adminOverview`): four stat cards and the pipeline status per district,
// from GET /v1/admin/overview (admins and reviewers).
export default async function OverviewPage() {
  const access = await guard("overview");
  if (access.denied) return access.denied;

  let data: AdminOverview;
  try {
    data = await adminGet<AdminOverview>("/v1/admin/overview");
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Overview" />;
  }
  const t = data.totals;
  const int = (n: number) => n.toLocaleString("en-US");

  return (
    <>
      <div className="astat-grid">
        <StatCard label="Parcels ingested" value={int(t.parcels)} note={`▲ ${data.municipality_name} pilot`} noteTone="up" />
        <StatCard
          label="Planning documents"
          value={int(t.documents)}
          note={`${int(t.documents_adopted)} adopted · ${int(t.documents_in_progress)} in progress`}
        />
        <StatCard label="Pending AI review" value={int(t.pending_review)} warn note="◐ needs expert sign-off" noteTone="warn" />
        <StatCard
          label="Paid orders"
          value={int(t.paid_orders)}
          small={`· ${formatEur(t.revenue_eur)}`}
          note={`▲ ${int(t.paid_orders_last_7_days)} this week`}
          noteTone="up"
        />
      </div>
      <AdminCard title="Pipeline status" sub="The repeatable ingestion methodology, per district">
        <DataTable<DistrictStatus>
          rows={data.districts}
          rowKey={(d) => d.zone_id}
          empty="No districts yet: import the zone list (python -m core.zones import) and publish."
          columns={[
            { key: "district", label: "District", render: (d) => d.name },
            { key: "documents", label: "Documents", mono: true, render: (d) => d.documents },
            { key: "extraction", label: "Extraction", render: (d) => extractionLabel(d.extraction) },
            { key: "review", label: "Expert review", mono: true, render: (d) => reviewLabel(d.review_pct) },
            {
              key: "live",
              label: "Live",
              render: (d) => {
                const chip = liveChip(d.live);
                return <StatusChip tone={chip.tone}>{chip.label}</StatusChip>;
              },
            },
          ]}
        />
      </AdminCard>
    </>
  );
}
