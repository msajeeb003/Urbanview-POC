import Form from "next/form";
import Link from "next/link";

import { OrderDrawer } from "@/components/admin/orders/order-drawer";
import { AdminCard, AdminUnavailable, StatusChip } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { relativeTime } from "@/lib/admin/format";
import { guard } from "@/lib/admin/guard";
import {
  customerLine,
  daysSince,
  emptyOrdersText,
  isManager,
  ORDER_STATUSES,
  orderQuery,
  ordersHref,
  parcelCells,
  parseOrderFilters,
  statusChip,
  versionText,
} from "@/lib/admin/orders";
import { staffZone } from "@/lib/admin/zone-server";
import { ApiError } from "@/lib/api/client";
import type { OrderDetail, OrderExpert, OrderList } from "@/lib/api/types";

// Orders (wireframe `adminOrders`): the manual fulfilment queue, newest first. Admins see every
// order, record payments and assign experts; an expert sees the orders assigned to them and
// uploads the report. Each row: reference, parcel (KO + number, the planned parcel), customer,
// placed, age, status, price, turnaround, the data version the customer saw (its number, `v12`:
// the label on hover), delivered, expert. The exact times on hover are the municipality's, named.
// `?order=<id>` opens the order's drawer.
export default async function OrdersPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const access = await guard("orders");
  if (access.denied) return access.denied;
  const filters = parseOrderFilters(await searchParams);
  const role = access.staff.role;
  const manager = isManager(role);

  let data: OrderList;
  let experts: OrderExpert[] = [];
  const zonePromise = staffZone();
  try {
    [data, experts] = await Promise.all([
      adminGet<OrderList>("/v1/admin/orders", orderQuery(filters)),
      manager ? adminGet<OrderExpert[]>("/v1/admin/orders/experts") : Promise.resolve([]),
    ]);
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Expert analysis orders" />;
  }

  let open: OrderDetail | null = null;
  let openProblem: string | null = null;
  if (filters.order) {
    try {
      open = await adminGet<OrderDetail>(`/v1/admin/orders/${filters.order}`);
    } catch (err) {
      openProblem =
        err instanceof AdminAccessDenied
          ? "This order is not assigned to you."
          : err instanceof ApiError && err.status === 404
            ? "No such order."
            : "The order could not be loaded.";
    }
  }
  const now = new Date();
  const zone = await zonePromise;
  const filtered = !!(filters.status || filters.expert || filters.q);

  return (
    <>
      <AdminCard
        title="Expert analysis orders"
        sub={manager ? `Manual fulfilment queue · ${data.total} order${data.total === 1 ? "" : "s"}` : "Manual fulfilment queue · the orders assigned to you"}
      >
        <Form action="/admin/orders" className="datafilters" role="search">
          <input type="search" name="q" defaultValue={filters.q} placeholder="Reference, e-mail, name or parcel" aria-label="Search" />
          <select name="status" defaultValue={filters.status ?? ""} aria-label="Status">
            <option value="">Any status</option>
            {ORDER_STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          {manager && (
            <select name="expert" defaultValue={filters.expert ?? ""} aria-label="Expert">
              <option value="">Any expert</option>
              {experts.map((x) => (
                <option key={x.user_id} value={x.user_id}>
                  {x.display_name ?? x.email}
                </option>
              ))}
            </select>
          )}
          <button type="submit" className="abtn sm">
            Filter
          </button>
          {filtered && (
            <Link className="abtn sm ghost" href="/admin/orders">
              Clear
            </Link>
          )}
        </Form>
        {data.items.length === 0 ? (
          <div className="admin-note">{emptyOrdersText(filters, !manager)}</div>
        ) : (
          <table className="tbl ordtbl">
            <thead>
              <tr>
                <th>Ref</th>
                <th>Parcel</th>
                <th>Customer</th>
                <th>Placed</th>
                <th>Days</th>
                <th>Status</th>
                <th>Price</th>
                <th title="Business days after the payment, and the expected date">Turnaround</th>
                <th title="The published data version the customer saw">Data</th>
                <th>Delivered</th>
                <th>Expert</th>
                <th aria-label="Open" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((o) => {
                const chip = statusChip(o.status);
                const href = ordersHref(filters, o.id);
                const parcel = parcelCells(o);
                return (
                  <tr key={o.id} className={filters.order === o.id ? "sel" : undefined}>
                    <td className="mono">{o.reference}</td>
                    <td className="mono">
                      {parcel.cadastral ?? "—"}
                      <span className="fmeta">{parcel.planned ? `urban parcel ${parcel.planned}` : "no urban parcel"}</span>
                    </td>
                    <td>
                      {customerLine(o)}
                      <span className="fmeta">{o.email}</span>
                    </td>
                    <td title={zone.named(o.placed_at)}>{relativeTime(o.placed_at, now, zone.zone)}</td>
                    <td className="mono">{daysSince(o.placed_at, now)}</td>
                    <td>
                      <span className="pillcell">
                        <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                        {o.email_alerts ? (
                          <span className="owarn" title="An e-mail to the customer bounced or failed: check the order's e-mails">
                            ⚠ e-mail
                          </span>
                        ) : null}
                      </span>
                    </td>
                    <td className="mono">€{o.price_eur}</td>
                    <td className="mono" title={`expected by ${o.expected_by}`}>
                      {o.turnaround_business_days} d
                    </td>
                    <td className="mono" title={o.data_version ?? undefined}>
                      {versionText(o) ?? "—"}
                    </td>
                    <td title={o.delivered_at ? zone.named(o.delivered_at) : undefined}>
                      {o.delivered_at ? relativeTime(o.delivered_at, now, zone.zone) : <span className="osub">—</span>}
                    </td>
                    <td>{o.assignee ? (o.assignee.display_name ?? o.assignee.email) : <span className="osub">—</span>}</td>
                    <td>
                      <Link className="abtn sm ghost" href={href} scroll={false}>
                        Open
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </AdminCard>
      {open && <OrderDrawer order={open} experts={experts} role={role} closeHref={ordersHref(filters, null)} />}
      {openProblem && (
        <div className="admin-note" role="status">
          {openProblem}{" "}
          <Link className="abtn sm ghost" href={ordersHref(filters, null)}>
            Close
          </Link>
        </div>
      )}
    </>
  );
}
