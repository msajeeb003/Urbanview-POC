import { AdminCard, AdminUnavailable, DataTable, StatusChip } from "@/components/admin/parts";
import {
  DEFAULT_DAYS,
  count,
  districtChip,
  districtName,
  parseRange,
  pctText,
  rangeLabel,
  rangeQuery,
  ratioText,
  stepLabel,
  uncoveredDemand,
  type District,
  type FunnelStep,
} from "@/lib/admin/analytics";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { guard } from "@/lib/admin/guard";
import type { AnalyticsDashboard } from "@/lib/api/types";

// Analytics (admins; the POC plan's aggregates: funnel, districts, intent counts, from
// GET /v1/admin/analytics) as plain tables, no dashboard. The dates are a plain GET form, so a view is a
// link. Most-searched districts include searches outside coverage, placed by their point (S6
// demand: where people look for plans that are not published yet).

type Params = Record<string, string | string[] | undefined>;

const int = (n: number) => n.toLocaleString("en-US");

export default async function AnalyticsPage({ searchParams }: { searchParams: Promise<Params> }) {
  const access = await guard("analytics");
  if (access.denied) return access.denied;
  const range = parseRange(await searchParams);

  let data: AnalyticsDashboard;
  try {
    data = await adminGet<AnalyticsDashboard>("/v1/admin/analytics", rangeQuery(range));
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Analytics" />;
  }
  const { totals, funnel, repeat_usage: repeat, interest, panel_to_financials: panels } = data;
  const demand = uncoveredDemand(data.districts);

  return (
    <>
      <AdminCard
        title="Analytics"
        sub={`${rangeLabel(data.range)} · ${count(totals.sessions, "session")} · anonymous events, no personal data`}
        action={
          <form className="audit-filters" method="get" action="/admin/analytics">
            <input type="date" name="from" defaultValue={range.from} aria-label="From" />
            <input type="date" name="to" defaultValue={range.to} aria-label="To" />
            <button type="submit" className="abtn sm">
              Show
            </button>
            {(range.from || range.to) && (
              <a className="abtn sm ghost" href="/admin/analytics">
                Last {DEFAULT_DAYS} days
              </a>
            )}
          </form>
        }
      >
      </AdminCard>

      <AdminCard title="Funnel" sub={`Sessions reaching each step · overall ${pctText(funnel.overall_conversion_pct)}`}>
        <DataTable<FunnelStep>
          rows={funnel.steps}
          rowKey={(s) => s.step}
          empty="No sessions in this range."
          columns={[
            { key: "step", label: "Step", render: (s) => stepLabel(s.step) },
            { key: "events", label: "Events", mono: true, render: (s) => s.event_names.join(" · ") },
            { key: "sessions", label: "Sessions", mono: true, render: (s) => int(s.sessions) },
            { key: "prev", label: "From previous", mono: true, render: (s) => pctText(s.conversion_from_previous_pct) },
            { key: "start", label: "From start", mono: true, render: (s) => pctText(s.conversion_from_start_pct) },
          ]}
        />
      </AdminCard>

      <AdminCard
        title="Most-searched districts"
        sub={
          demand.searches
            ? `Searches and parcel picks per district · ${count(demand.searches, "search", "searches")} outside coverage, in ${count(demand.districts, "district")}`
            : "Searches and parcel picks per district"
        }
      >
        <DataTable<District>
          rows={data.districts}
          rowKey={(d) => d.zone_id ?? "none"}
          empty="No searches in this range."
          columns={[
            { key: "district", label: "District", render: (d) => districtName(d) },
            {
              key: "coverage",
              label: "Coverage",
              render: (d) => {
                const chip = districtChip(d);
                return chip ? <StatusChip tone={chip.tone}>{chip.label}</StatusChip> : "—";
              },
            },
            { key: "searches", label: "Searches", mono: true, render: (d) => int(d.searches) },
            { key: "uncovered", label: "Outside coverage", mono: true, render: (d) => int(d.uncovered_searches ?? 0) },
            { key: "selections", label: "Parcel picks", mono: true, render: (d) => int(d.selections) },
            { key: "sessions", label: "Sessions", mono: true, render: (d) => int(d.sessions) },
            { key: "share", label: "Share", mono: true, render: (d) => pctText(d.share_pct) },
          ]}
        />
      </AdminCard>

      <AdminCard title="Repeat usage and interest" sub="Validation signals of the pilot">
        <DataTable<{ key: string; label: string; value: string; note: string }>
          rows={[
            {
              key: "returning",
              label: "Returning sessions",
              value: `${int(repeat.returning_sessions)} of ${int(repeat.sessions)}`,
              note: pctText(repeat.repeat_usage_rate_pct),
            },
            {
              key: "per-client",
              label: "Sessions per browser",
              value: ratioText(repeat.sessions_per_client),
              note: `${int(repeat.clients_at_target)} of ${int(repeat.clients)} at ${repeat.target_sessions_per_user}+ (${pctText(repeat.clients_at_target_pct)})`,
            },
            {
              key: "market",
              label: "“Unlock full market data”",
              value: int(interest.market_data_interest.events),
              note: count(interest.market_data_interest.sessions, "session"),
            },
            {
              key: "ai",
              label: "“Ask about this site”",
              value: int(interest.ai_interest.events),
              note: count(interest.ai_interest.sessions, "session"),
            },
            {
              key: "financials",
              label: "Parcel panels reaching the financials",
              value: `${int(panels.pairs_reaching_financials)} of ${int(panels.panel_view_pairs)}`,
              note: pctText(panels.reaching_financials_pct),
            },
          ]}
          rowKey={(r) => r.key}
          empty="No events in this range."
          columns={[
            { key: "signal", label: "Signal", render: (r) => r.label },
            { key: "value", label: "Count", mono: true, render: (r) => r.value },
            { key: "note", label: "", mono: true, render: (r) => r.note },
          ]}
        />
      </AdminCard>
    </>
  );
}
