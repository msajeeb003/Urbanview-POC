/**
 * The rules of the Orders tab (`/admin/orders`): the status chips, which action the order's status
 * and the member's role allow (the API's guards, mirrored so a button is disabled with the reason
 * instead of failing), the list filters, the map link of the ordered parcel, and reading the
 * snapshot the customer saw.
 *
 * Status flow (the API's `TRANSITIONS`): pending_payment → paid → in_progress → delivered;
 * payment_failed from pending_payment ("Payment not received"; it can still be paid); refunded from
 * paid, in_progress or delivered (a delivered order never goes back to work). A paid order moves
 * to in_progress when an expert is assigned (never before the payment); uploading the report
 * delivers it and e-mails the customer; a delivered report can be replaced (a note says why; the
 * new link is e-mailed again). Admins manage orders (the pilot scope's roles); an expert sees only
 * the orders assigned to them and only uploads the report.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { OrderDetail, OrderEmail, OrderEvent, OrderStatus, OrderSummary } from "@/lib/api/types";

import type { StaffRole } from "./sections";

export const ORDER_STATUSES: readonly { value: OrderStatus; label: string; tone: ChipTone }[] = [
  { value: "pending_payment", label: "Pending payment", tone: "pend" },
  { value: "payment_failed", label: "Payment not received", tone: "rev" },
  { value: "paid", label: "Paid", tone: "pend" },
  { value: "in_progress", label: "In progress", tone: "rev" },
  { value: "delivered", label: "Delivered", tone: "ok" },
  { value: "refunded", label: "Refunded", tone: "rev" },
];

export function statusChip(status: OrderStatus): { tone: ChipTone; label: string } {
  const found = ORDER_STATUSES.find((s) => s.value === status);
  return found ? { tone: found.tone, label: found.label } : { tone: "rev", label: status };
}

export function statusLabel(status: string): string {
  return ORDER_STATUSES.find((s) => s.value === status)?.label ?? status.replace(/_/g, " ");
}

// --- what may happen now ---------------------------------------------------------------------------

export type OrderAction = "receive" | "notReceived" | "refund" | "assign" | "upload";

export interface Allowed {
  /** Shown at all (experts see only the report upload). */
  visible: boolean;
  enabled: boolean;
  /** Why it is disabled (the tooltip). */
  reason?: string;
}

// the pilot scope's roles: admins manage orders; experts deliver the ones assigned to them
const MANAGERS: readonly StaffRole[] = ["admin"];

export function isManager(role: StaffRole | null | undefined): boolean {
  return !!role && MANAGERS.includes(role);
}

/** The API's guards for each action, with the sentence a disabled button shows. */
export function allowed(
  order: Pick<OrderDetail, "status" | "assignee">,
  action: OrderAction,
  role: StaffRole | null | undefined,
): Allowed {
  const manager = isManager(role);
  const status = order.status;
  const is = statusLabel(status).toLowerCase();
  const no = (reason: string, visible = true): Allowed => ({ visible, enabled: false, reason });
  switch (action) {
    case "receive":
    case "notReceived":
      if (!manager) return no("Admins record payments.", false);
      return status === "pending_payment" || status === "payment_failed"
        ? { visible: true, enabled: true }
        : no(`Payments are recorded while the order awaits payment; this one is ${is}.`);
    case "refund":
      if (!manager) return no("Admins record refunds.", false);
      return status === "paid" || status === "in_progress" || status === "delivered"
        ? { visible: true, enabled: true }
        : no(`Only a paid order (in progress or delivered too) can be refunded; this one is ${is}.`);
    case "assign":
      if (!manager) return no("Admins assign experts.", false);
      if (status === "paid" || status === "in_progress") return { visible: true, enabled: true };
      return no(
        status === "pending_payment" || status === "payment_failed"
          ? "An expert is assigned once the payment is received."
          : `A ${is} order is closed.`,
      );
    case "upload":
      if (status === "in_progress" || status === "delivered") return { visible: true, enabled: true };
      return no(
        status === "refunded"
          ? "The order was refunded."
          : "The report is uploaded once the order is paid and in progress.",
      );
    default:
      return no("Not available.");
  }
}

// --- list ---------------------------------------------------------------------------------------------

export function daysSince(iso: string, now: Date = new Date()): number {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.floor((now.getTime() - t) / 86_400_000));
}

/** "Marko Petrović · Adria d.o.o." */
export function customerLine(order: Pick<OrderSummary, "customer_name" | "company_name" | "purchaser_type">): string {
  return order.purchaser_type === "legal_entity" && order.company_name
    ? `${order.customer_name} · ${order.company_name}`
    : order.customer_name;
}

/** The public map at the ordered parcel (its Parcel ID); null when the order has none. */
export function mapHref(location: { parcel_type: string; parcel_id: number; cadastral_parcel_id?: number | null }): string | null {
  const id = location.cadastral_parcel_id ?? (location.parcel_type === "cadastral" ? location.parcel_id : null);
  return id ? `/?parcel=${id}` : null;
}

export interface OrderFilters {
  status: OrderStatus | null;
  expert: number | null;
  q: string;
  order: number | null;
}

type Params = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string {
  return (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
}

function positive(value: string): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

export function parseOrderFilters(params: Params): OrderFilters {
  const status = first(params.status);
  return {
    status: ORDER_STATUSES.some((s) => s.value === status) ? (status as OrderStatus) : null,
    expert: positive(first(params.expert)),
    q: first(params.q).slice(0, 100),
    order: positive(first(params.order)),
  };
}

export function orderQuery(filters: OrderFilters): Record<string, string | number | undefined> {
  return {
    status: filters.status ?? undefined,
    assignee_user_id: filters.expert ?? undefined,
    search: filters.q || undefined,
    limit: 200,
  };
}

/** The list's URL with one order open (the drawer) or none. */
export function ordersHref(filters: OrderFilters, open: number | null = filters.order): string {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.expert) params.set("expert", String(filters.expert));
  if (filters.q) params.set("q", filters.q);
  if (open) params.set("order", String(open));
  const query = params.toString();
  return query ? `/admin/orders?${query}` : "/admin/orders";
}

export function emptyOrdersText(filters: OrderFilters, expert: boolean): string {
  if (filters.status || filters.expert || filters.q) return "No order matches these filters.";
  return expert ? "No orders are assigned to you yet." : "No orders yet. They appear here as soon as a customer places one.";
}

// --- e-mails and timeline ---------------------------------------------------------------------------

const TEMPLATE_NAMES: Record<string, string> = {
  payment_instructions: "Payment instructions",
  order_delivered: "Report delivered",
  magic_link: "Sign-in link",
};

export function emailName(template: string): string {
  return TEMPLATE_NAMES[template] ?? template.replace(/_/g, " ");
}

export function emailChip(email: Pick<OrderEmail, "status">): { tone: ChipTone; label: string } {
  switch (email.status) {
    case "sent":
      return { tone: "ok", label: "Sent" };
    case "queued":
      return { tone: "pend", label: "Queued" };
    case "suppressed":
      return { tone: "rev", label: "Not sent" };
    default:
      return { tone: "rev", label: "Failed" };
  }
}

function money(value: unknown): string | null {
  return typeof value === "number" ? `€${value.toFixed(2).replace(/\.00$/, "")}` : null;
}

export interface RefundFacts {
  /** "€200" */
  amount: string | null;
  /** The date the money went back, as recorded (`2026-10-02`). */
  on: string | null;
  reference: string | null;
}

/**
 * What went back to the customer: the refund's amount, date and bank reference. They are
 * recorded with the status change (the order's audit entry), so the Payment section reads them
 * from the timeline instead of leaving them to be found there. Null for an order not refunded.
 */
export function refundOf(order: Pick<OrderDetail, "timeline">): RefundFacts | null {
  const event = [...(order.timeline ?? [])]
    .reverse()
    .find((e) => e.action === "order.status" && (e.after as Record<string, unknown> | null)?.status === "refunded");
  if (!event) return null;
  const d = (event.details ?? {}) as Record<string, unknown>;
  return {
    amount: money(d.refund_amount_eur),
    on: typeof d.refunded_on === "string" && d.refunded_on ? d.refunded_on : null,
    reference: typeof d.bank_reference === "string" && d.bank_reference ? d.bank_reference : null,
  };
}

/** A refund never exceeds what was received (the API refuses it too); null = fine. */
export function refundProblem(order: Pick<OrderDetail, "payment_amount_eur">, amount: number | null): string | null {
  const received = order.payment_amount_eur;
  return amount != null && received != null && amount > received
    ? `A refund cannot be more than the ${money(received)} received.`
    : null;
}

/**
 * Where the customer said they pay from, with the account the order showed them: staff look for
 * the transfer on that account (the domestic one, or the one behind the IBAN). An order placed
 * before the form asked was shown the domestic details.
 */
export function paymentOriginText(origin: OrderDetail["payment_origin"], country: string | null | undefined): string {
  if (origin === "international") return "Another country · shown the IBAN and SWIFT / BIC";
  const shown = "shown the domestic account number";
  return origin === "domestic" ? `${country ?? "This country"} · ${shown}` : `Not asked · ${shown}`;
}

/**
 * The published data version an order was placed on, as every screen names a version: its number
 * (`v12`). The label is free text typed at each publish ("first-publish", "stara-varos-live"), so
 * it only stands in for an order whose version is no longer known.
 */
export function versionText(order: Pick<OrderSummary, "data_version" | "data_version_no">): string | null {
  if (order.data_version_no != null) return `v${order.data_version_no}`;
  return order.data_version ?? null;
}

/** One line per audit entry of the order, in plain words. */
export function eventLine(event: Pick<OrderEvent, "action" | "details" | "before" | "after">): string {
  const d = (event.details ?? {}) as Record<string, unknown>;
  const after = (event.after ?? {}) as Record<string, unknown>;
  const before = (event.before ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  switch (event.action) {
    case "order.payment":
      parts.push("Payment received");
      if (money(d.amount_eur)) parts.push(money(d.amount_eur)!);
      if (d.received_on) parts.push(`on ${d.received_on}`);
      if (d.bank_reference) parts.push(`ref ${d.bank_reference}`);
      break;
    case "order.create":
      parts.push("Order placed by the customer");
      break;
    case "order.payment_check":
      parts.push("Payment not received");
      break;
    case "order.assign":
      parts.push(`Assigned to ${after.assignee_email ?? "an expert"}`);
      break;
    case "order.report":
      parts.push(`Report${typeof d.version === "number" && d.version > 1 ? ` v${d.version}` : ""} uploaded`);
      if (d.filename) parts.push(String(d.filename));
      break;
    case "order.status": {
      const to = typeof after.status === "string" ? statusLabel(after.status) : "?";
      const from = typeof before.status === "string" ? statusLabel(before.status) : null;
      parts.push(from ? `${from} → ${to}` : to);
      if (after.status === "refunded") {
        if (money(d.refund_amount_eur)) parts.push(`refund ${money(d.refund_amount_eur)}`);
        if (d.refunded_on) parts.push(`on ${d.refunded_on}`);
        if (d.bank_reference) parts.push(`ref ${d.bank_reference}`);
      }
      break;
    }
    default:
      parts.push(event.action.replace(/^order\./, "").replace(/_/g, " "));
  }
  return parts.join(" · ");
}

// --- the snapshot the customer saw ---------------------------------------------------------------------

export interface SnapshotRow {
  label: string;
  value: string;
  note?: string;
}

function num(n: number, unit?: string | null): string {
  if (unit === "EUR" || unit === "€") {
    // whole euros, the sign before the symbol
    const euros = Math.round(n);
    return `${euros < 0 ? "−" : ""}€${Math.abs(euros).toLocaleString("en-GB")}`;
  }
  const s = Number.isInteger(n) ? n.toLocaleString("en-GB") : n.toLocaleString("en-GB", { maximumFractionDigits: 2 });
  if (!unit) return s;
  if (unit === "%") return `${s}%`;
  return `${s} ${unit}`;
}

type Loose = Record<string, unknown>;

/** Group 1 as the customer saw it: every stated or computed planning value. */
export function snapshotPlanning(snapshot: Loose): SnapshotRow[] {
  const fields = ((snapshot.planning as Loose | undefined)?.fields as Loose[] | undefined) ?? [];
  return fields
    .filter((f) => f.status === "stated" || f.status === "computed")
    .map((f) => {
      const value = f.value;
      const unit = (f.unit as string | null | undefined) ?? null;
      const source = f.source as Loose | undefined;
      return {
        label: String(f.label_en ?? f.key),
        value: typeof value === "number" ? num(value, unit) : String(value ?? "—"),
        note: source?.page ? `p.${source.page}` : f.status === "computed" ? "computed" : undefined,
      };
    });
}

// the order the drawer lists Group 2 in: areas, the costs, then what they are set against
const FIGURE_ORDER = [
  "max_gfa_m2",
  "max_coverage_area_m2",
  "saleable_area_m2",
  "land_value_eur",
  "design_documentation_eur",
  "construction_cost_eur",
  "total_cost_eur",
  "revenue_eur",
  "profit_eur",
  "roi_pct",
];

/**
 * Group 2 as the customer saw it: the ranges (low – expected – high). The payload's `fields` plus
 * the cost rows they do not repeat (land value, design & documentation, total cost).
 */
export function snapshotFeasibility(snapshot: Loose): SnapshotRow[] {
  const block = snapshot.feasibility as Loose | null | undefined;
  const own = (block?.fields as Loose[] | undefined) ?? [];
  const keys = new Set(own.map((f) => f.key));
  const costs = ((block?.cost_rows as Loose[] | undefined) ?? []).filter((r) => !keys.has(r.key));
  const place = (f: Loose) => {
    const at = FIGURE_ORDER.indexOf(String(f.key));
    return at === -1 ? FIGURE_ORDER.length : at;
  };
  const fields = [...own, ...costs].sort((a, b) => place(a) - place(b));
  return fields.map((f) => {
    const unit = (f.unit as string | undefined) ?? "";
    if (f.status !== "ok") return { label: String(f.label_en ?? f.key), value: "cannot calculate", note: (f.reason_en as string) ?? undefined };
    const low = f.low as number | null | undefined;
    const expected = f.expected as number | null | undefined;
    const high = f.high as number | null | undefined;
    const value =
      f.range_kind === "deterministic" || low == null || high == null
        ? num(expected ?? 0, unit)
        : `${num(low, unit)} – ${num(expected ?? 0, unit)} – ${num(high, unit)}`;
    return { label: String(f.label_en ?? f.key), value };
  });
}

/** The assumptions the figures used, with what the customer changed. */
export function snapshotAssumptions(snapshot: Loose, edits: Record<string, unknown>): SnapshotRow[] {
  const a = snapshot.assumptions as Loose | null | undefined;
  if (!a) return [];
  const edited = (key: string) => (key in edits ? "changed by the customer" : undefined);
  const rows: SnapshotRow[] = [];
  if (typeof a.construction_cost_eur_m2 === "number") {
    rows.push({ label: "Construction cost", value: `€${a.construction_cost_eur_m2}/m²`, note: edited("construction_cost_eur_m2") ?? edited("construction_cost_per_m2") });
  }
  if (typeof a.sale_price_eur_m2 === "number") {
    rows.push({ label: "Sale price", value: `€${a.sale_price_eur_m2}/m²`, note: edited("sale_price_eur_m2") ?? edited("selling_price_per_m2") });
  }
  if (typeof a.saleable_share === "number") {
    rows.push({ label: "Saleable share", value: `${Math.round(a.saleable_share * 100)}%`, note: edited("saleable_share") });
  }
  if (typeof a.land_rate_eur_m2 === "number") rows.push({ label: "Land value", value: `€${a.land_rate_eur_m2}/m²` });
  if (typeof a.design_documentation_eur_m2 === "number") {
    rows.push({ label: "Design & documentation", value: `€${a.design_documentation_eur_m2}/m²` });
  }
  if (a.market_source) rows.push({ label: "Market data", value: String(a.market_source), note: a.market_source_date ? String(a.market_source_date) : undefined });
  return rows;
}

/** The ordered parcel as the drawer shows it: "#1042/3 · Podgorica I" (cadastral), "UP 12" (urban). */
export function parcelLine(location: { parcel_type: string; parcel_label: string }): string {
  const label = location.parcel_label.trim();
  const ko = /^KO (.+?), (.+)$/.exec(label);
  if (ko) return `#${ko[2]} · ${ko[1]}`;
  if (location.parcel_type === "urban") return /^up\b/i.test(label) ? label : `UP ${label}`;
  return label.startsWith("#") ? label : `#${label}`;
}

/**
 * The queue's parcel cell: the cadastral parcel ("#1042 · Podgorica I", from the order's
 * `ko_and_number`) and the planned parcel the figures used ("UP 12"), whichever the order has.
 */
export function parcelCells(order: Pick<OrderSummary, "ko_and_number" | "planned_parcel">): { cadastral: string | null; planned: string | null } {
  return {
    cadastral: order.ko_and_number ? parcelLine({ parcel_type: "cadastral", parcel_label: order.ko_and_number }) : null,
    planned: order.planned_parcel ? parcelLine({ parcel_type: "urban", parcel_label: order.planned_parcel }) : null,
  };
}
