import { AdminCard, AdminUnavailable, DataTable, StatusChip } from "@/components/admin/parts";
import {
  DEFAULT_DAYS,
  count,
  districtChip,
  districtName,
  parseRange,
  pctText,
  positionText,
  rangeLabel,
  rangeQuery,
  stepLabel,
  type FunnelStep,
  type OrderStatusCount,
  type UncoveredHit,
  type ZoneHits,
} from "@/lib/admin/analytics";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { statusLabel } from "@/lib/admin/orders";
import { guard } from "@/lib/admin/guard";
import type { AnalyticsDashboard } from "@/lib/api/types";
import { formatEur } from "@/lib/format";

// Analytics (admins; the POC plan's aggregates from GET /v1/admin/analytics) as plain tables, no
// dashboard: the funnel from map to paid order, orders by status, the top zones and where searches
// outside coverage landed (S6 demand), repeat visitors and the two intent buttons. The dates are a
// plain GET form, so a view is a link.

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
  const { funnel, orders, repeat_sessions: repeat, intent_counts: intent } = data;

  return (
    <>
      <AdminCard
        title="Analytics"
        sub={`${rangeLabel(data.range)} · ${count(repeat.sessions, "session")} · anonymous events, no personal data`}
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
            {
              key: "events",
              label: "Counted from",
              mono: true,
              render: (s) => (s.event_names.length ? s.event_names.join(" · ") : "orders marked paid"),
            },
            { key: "sessions", label: "Sessions", mono: true, render: (s) => int(s.sessions) },
            { key: "prev", label: "From previous", mono: true, render: (s) => pctText(s.conversion_from_previous_pct) },
            { key: "start", label: "From start", mono: true, render: (s) => pctText(s.conversion_from_start_pct) },
          ]}
        />
      </AdminCard>

      <AdminCard title="Orders by status" sub={`${count(orders.placed, "order")} placed in this range`}>
        <DataTable<OrderStatusCount>
          rows={orders.by_status}
          rowKey={(o) => o.status}
          empty="No orders in this range."
          columns={[
            { key: "status", label: "Status", render: (o) => statusLabel(o.status) },
            { key: "orders", label: "Orders", mono: true, render: (o) => int(o.orders) },
            { key: "amount", label: "Amount", mono: true, render: (o) => formatEur(o.amount_eur) },
          ]}
        />
      </AdminCard>

      <AdminCard title="Most-searched districts" sub="Searches and parcel picks per district, searches outside coverage placed by their point">
        <DataTable<ZoneHits>
          rows={data.top_zones}
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
            { key: "uncovered", label: "Outside coverage", mono: true, render: (d) => int(d.uncovered_searches) },
            { key: "selections", label: "Parcel picks", mono: true, render: (d) => int(d.selections) },
            { key: "sessions", label: "Sessions", mono: true, render: (d) => int(d.sessions) },
            { key: "share", label: "Share", mono: true, render: (d) => pctText(d.share_pct) },
          ]}
        />
      </AdminCard>

      <AdminCard title="Searches outside coverage" sub="Where visitors looked for a plan that is not published: by position, ≈ 110 m">
        <DataTable<UncoveredHit>
          rows={data.uncovered_hits}
          rowKey={(h) => `${h.lat},${h.lng}`}
          empty="No searches outside coverage in this range."
          columns={[
            { key: "position", label: "Position", mono: true, render: (h) => positionText(h) },
            { key: "searches", label: "Searches", mono: true, render: (h) => int(h.searches) },
            { key: "sessions", label: "Sessions", mono: true, render: (h) => int(h.sessions) },
          ]}
        />
      </AdminCard>

      <AdminCard title="Repeat visits and interest" sub="Validation signals of the pilot">
        <DataTable<{ key: string; label: string; value: string; note: string }>
          rows={[
            {
              key: "repeat",
              label: `Visitors with ${repeat.min_sessions}+ sessions`,
              value: `${int(repeat.repeat_visitors)} of ${int(repeat.visitors)}`,
              note: `${pctText(repeat.repeat_visitors_pct)} · ${count(repeat.repeat_sessions, "session")}`,
            },
            {
              key: "market",
              label: "“Unlock full market data”",
              value: int(intent.market_data_interest.events),
              note: count(intent.market_data_interest.sessions, "session"),
            },
            {
              key: "ai",
              label: "“Ask about this site”",
              value: int(intent.ai_interest.events),
              note: count(intent.ai_interest.sessions, "session"),
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
