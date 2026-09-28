import { describe, expect, it } from "vitest";

import type { OrderDetail, OrderStatus } from "@/lib/api/types";

import {
  allowed,
  customerLine,
  daysSince,
  eventLine,
  mapHref,
  ordersHref,
  parcelLine,
  parseOrderFilters,
  snapshotAssumptions,
  snapshotFeasibility,
  snapshotPlanning,
  statusChip,
} from "./orders";

const order = (status: OrderStatus, assigned = false) =>
  ({ status, assignee: assigned ? { user_id: 6, email: "expert@urbanview.test" } : null }) as Pick<OrderDetail, "status" | "assignee">;

describe("status flow", () => {
  it("chips every status", () => {
    expect(statusChip("pending_payment")).toEqual({ tone: "pend", label: "Pending payment" });
    expect(statusChip("delivered").tone).toBe("ok");
    expect(statusChip("refunded").tone).toBe("rev");
    expect(statusChip("payment_failed")).toEqual({ tone: "rev", label: "Payment not received" });
  });

  it("mirrors the API's guards and says why an action is not available", () => {
    expect(allowed(order("pending_payment"), "receive", "admin").enabled).toBe(true);
    expect(allowed(order("paid"), "receive", "admin")).toMatchObject({ enabled: false, reason: expect.stringMatching(/awaits payment/) });
    expect(allowed(order("paid"), "refund", "admin").enabled).toBe(true);
    expect(allowed(order("in_progress"), "refund", "admin").enabled).toBe(true);
    expect(allowed(order("delivered"), "refund", "admin").enabled).toBe(false);
    expect(allowed(order("pending_payment"), "refund", "admin").enabled).toBe(false);
    // a failed payment can still be received (or checked again), never refunded
    expect(allowed(order("payment_failed"), "receive", "admin").enabled).toBe(true);
    expect(allowed(order("payment_failed"), "notReceived", "admin").enabled).toBe(true);
    expect(allowed(order("pending_payment"), "receive", "reviewer").visible).toBe(false); // no order access
    expect(allowed(order("payment_failed"), "refund", "admin").enabled).toBe(false);
    expect(allowed(order("paid"), "start", "admin")).toMatchObject({ enabled: false, reason: "Assign an expert first." });
    expect(allowed(order("paid", true), "start", "admin").enabled).toBe(true);
    expect(allowed(order("delivered"), "assign", "admin").enabled).toBe(false);
    expect(allowed(order("in_progress"), "upload", "expert").enabled).toBe(true);
    expect(allowed(order("delivered"), "upload", "expert").enabled).toBe(true); // replace, with a note
    expect(allowed(order("paid"), "upload", "admin").enabled).toBe(false);
  });

  it("gives experts the report upload only", () => {
    expect(allowed(order("pending_payment"), "receive", "expert").visible).toBe(false);
    expect(allowed(order("paid"), "refund", "expert").visible).toBe(false);
    expect(allowed(order("paid"), "assign", "expert").visible).toBe(false);
    expect(allowed(order("in_progress"), "upload", "expert").visible).toBe(true);
  });
});

describe("the queue", () => {
  it("writes the parcel, the customer and the age", () => {
    expect(parcelLine({ parcel_type: "cadastral", parcel_label: "KO Podgorica I, 1042/3" })).toBe("#1042/3 · Podgorica I");
    expect(parcelLine({ parcel_type: "urban", parcel_label: "12" })).toBe("UP 12");
    expect(customerLine({ customer_name: "Marko P.", company_name: "Adria d.o.o.", purchaser_type: "legal_entity" })).toBe("Marko P. · Adria d.o.o.");
    expect(customerLine({ customer_name: "Ana V.", company_name: null, purchaser_type: "individual" })).toBe("Ana V.");
    expect(daysSince("2026-09-20T10:00:00Z", new Date("2026-09-27T09:00:00Z"))).toBe(6);
  });

  it("links the map at the parcel, urban orders through their cadastral parcel", () => {
    expect(mapHref({ parcel_type: "cadastral", parcel_id: 1001 })).toBe("/?parcel=1001");
    expect(mapHref({ parcel_type: "urban", parcel_id: 7, cadastral_parcel_id: 1001 })).toBe("/?parcel=1001");
    expect(mapHref({ parcel_type: "urban", parcel_id: 7, cadastral_parcel_id: null })).toBeNull();
  });

  it("keeps the filters and the open order in the URL", () => {
    const filters = parseOrderFilters({ status: "paid", expert: "6", q: " UV-PODI ", order: "12" });
    expect(filters).toEqual({ status: "paid", expert: 6, q: "UV-PODI", order: 12 });
    expect(ordersHref(filters)).toBe("/admin/orders?status=paid&expert=6&q=UV-PODI&order=12");
    expect(ordersHref(filters, null)).toBe("/admin/orders?status=paid&expert=6&q=UV-PODI");
    expect(parseOrderFilters({ status: "lost" }).status).toBeNull();
  });
});

describe("timeline and snapshot", () => {
  it("writes the audit entries in plain words", () => {
    expect(
      eventLine({ action: "order.payment", details: { amount_eur: 200, received_on: "2026-09-25", bank_reference: "BANK-7" }, before: null, after: null }),
    ).toBe("Payment received · €200 · on 2026-09-25 · ref BANK-7");
    expect(
      eventLine({
        action: "order.status",
        details: { refund_amount_eur: 200, refunded_on: "2026-09-26", bank_reference: "REFUND-8" },
        before: { status: "paid" },
        after: { status: "refunded" },
      }),
    ).toBe("Paid → Refunded · refund €200 · on 2026-09-26 · ref REFUND-8");
    expect(eventLine({ action: "order.report", details: { version: 2, filename: "r.pdf" }, before: null, after: null })).toBe("Report v2 uploaded · r.pdf");
    expect(eventLine({ action: "order.create", details: {}, before: null, after: null })).toBe("Order placed by the customer");
  });

  it("reads what the customer saw", () => {
    const snapshot = {
      planning: {
        fields: [
          { key: "max_far", label_en: "Max floor area ratio (II)", status: "stated", value: 2.5, unit: null, source: { page: 14 } },
          { key: "max_height_m", label_en: "Max building height", status: "not_stated", value: null },
          { key: "max_gfa_m2", label_en: "Max gross floor area", status: "computed", value: 3000, unit: "m²" },
        ],
      },
      feasibility: {
        fields: [
          { key: "roi_pct", label_en: "ROI", unit: "%", range_kind: "range", status: "ok", low: 8, expected: 14.5, high: 21 },
          { key: "saleable_area_m2", label_en: "Saleable area", unit: "m²", range_kind: "deterministic", status: "ok", expected: 2100 },
        ],
      },
      assumptions: { saleable_share: 0.75, construction_cost_eur_m2: 850, sale_price_eur_m2: 1900, market_source: "Realitica" },
    };
    expect(snapshotPlanning(snapshot)).toEqual([
      { label: "Max floor area ratio (II)", value: "2.5", note: "p.14" },
      { label: "Max gross floor area", value: "3,000 m²", note: "computed" },
    ]);
    expect(snapshotFeasibility(snapshot)).toEqual([
      { label: "ROI", value: "8% – 14.5% – 21%" },
      { label: "Saleable area", value: "2,100 m²" },
    ]);
    const assumptions = snapshotAssumptions(snapshot, { saleable_share: 0.75 });
    expect(assumptions.find((r) => r.label === "Saleable share")).toEqual({ label: "Saleable share", value: "75%", note: "changed by the customer" });
    expect(assumptions.find((r) => r.label === "Construction cost")?.note).toBeUndefined();
  });
});
