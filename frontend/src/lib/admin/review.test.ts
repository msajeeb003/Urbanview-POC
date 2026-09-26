import { describe, expect, it } from "vitest";

import type { ReviewCounters, ReviewItem } from "@/lib/api/types";

import {
  countAfter,
  editorFor,
  explainReviewProblem,
  formatValue,
  itemTitle,
  nextPending,
  parseCorrection,
  parseReviewFilters,
  progressOf,
  reviewHrefFor,
  reviewQuery,
  sameParcel,
  samePage,
  sourceLine,
  statusChip,
  targetLabel,
} from "./review";

function item(over: Partial<ReviewItem> & { id: number }): ReviewItem {
  const base = {
    status: "pending",
    parameter_key: "max_far",
    label_en: "Max floor area ratio (II)",
    label_me: "Indeks izgrađenosti (II)",
    value_type: "number",
    field_unit: null,
    extracted: { number: 2, text: null, unit: null },
    amended: null,
    effective: { number: 2, text: null, unit: null },
    target: { entity_type: "urban_parcel", urban_parcel_id: 7, urban_parcel_number: "12", matched: true },
    source: { document_id: 3, document_name: "DUP Novi Grad", page: 14, file_id: 9 },
    extracted_by: "llm:claude-sonnet-5",
    extracted_at: "2026-09-26T10:00:00Z",
    published: false,
    superseded: false,
    flags: [],
  };
  return { ...base, ...over } as unknown as ReviewItem;
}

describe("how an item reads", () => {
  it("formats values as the wireframe's chips", () => {
    expect(formatValue({ number: 2, unit: null }, "number")).toBe("2.0");
    expect(formatValue({ number: 55, unit: "%" }, "number")).toBe("55%");
    expect(formatValue({ number: 27.5, unit: "m" }, "number")).toBe("27.5 m");
    expect(formatValue({ text: "P+5+Pk" }, "text")).toBe("P+5+Pk");
    expect(formatValue(null, "text")).toBe("—");
  });

  it("names the target as the plan does", () => {
    expect(targetLabel({ entity_type: "urban_parcel", urban_parcel_number: "12", matched: true })).toBe("UP 12");
    expect(targetLabel({ entity_type: "urban_parcel", label: "UP 3a", matched: false })).toBe("UP 3a");
    expect(targetLabel({ entity_type: "block", block_ref: "Blok VII", matched: true })).toBe("Blok VII");
    expect(targetLabel({ entity_type: "document", matched: true })).toBe("whole plan");
    const it1 = item({ id: 1 });
    expect(itemTitle(it1)).toBe("Max floor area ratio (II) — UP 12");
    expect(sourceLine(it1)).toBe("DUP Novi Grad · p.14");
  });

  it("chips published, amended and pending items", () => {
    expect(statusChip({ status: "amended", published: false })).toEqual({ tone: "ok", label: "Amended" });
    expect(statusChip({ status: "approved", published: true }).label).toBe("Published");
    expect(statusChip({ status: "pending", published: false }).tone).toBe("pend");
  });
});

describe("corrections", () => {
  it("gives each parameter its editor", () => {
    expect(editorFor(item({ id: 1 }))).toEqual({ kind: "number", unit: null });
    expect(editorFor(item({ id: 2, parameter_key: "max_floors", value_type: "text" }))).toEqual({ kind: "floors" });
    expect(editorFor(item({ id: 3, parameter_key: "land_use", value_type: "text" }))).toEqual({ kind: "choice", field: "land_use" });
    expect(editorFor(item({ id: 4, parameter_key: "max_site_coverage_pct", field_unit: "%", extracted: { number: 55, unit: "%" } }))).toEqual({
      kind: "number",
      unit: "%",
    });
  });

  it("checks a correction the way the API types it", () => {
    expect(parseCorrection({ kind: "number", unit: null }, "2,5", null)).toEqual({ ok: true, value: 2.5, unit: null });
    expect(parseCorrection({ kind: "number", unit: "%" }, "55 %", "%")).toEqual({ ok: true, value: 55, unit: "%" });
    expect(parseCorrection({ kind: "number", unit: "%" }, "140", "%").ok).toBe(false);
    expect(parseCorrection({ kind: "number", unit: null }, "two", null).ok).toBe(false);
    expect(parseCorrection({ kind: "floors" }, " P + 5 + Pk ", null)).toEqual({ ok: true, value: "P+5+Pk", unit: null });
    expect(parseCorrection({ kind: "floors" }, "S+P+4", null).ok).toBe(true);
    expect(parseCorrection({ kind: "floors" }, "five floors", null).ok).toBe(false);
    expect(parseCorrection({ kind: "text" }, "   ", null).ok).toBe(false);
  });
});

describe("moving through the queue", () => {
  const list = [
    item({ id: 1, status: "approved" }),
    item({ id: 2 }),
    item({ id: 3, published: true }),
    item({ id: 4, source: { document_id: 3, document_name: "DUP Novi Grad", page: 15, file_id: 9 } as ReviewItem["source"] }),
  ];

  it("finds the next pending item, wrapping round", () => {
    expect(nextPending(list, 1)).toBe(3);
    expect(nextPending(list, 3)).toBe(1);
    expect(nextPending([item({ id: 9, status: "approved" })], 0)).toBeNull();
  });

  it("groups pending items by page and by parcel for bulk approval", () => {
    expect(samePage(list, list[1]).map((i) => i.id)).toEqual([2]);
    expect(sameParcel(list, list[1]).map((i) => i.id)).toEqual([2, 4]);
  });

  it("counts progress and moves one decision between counters", () => {
    const counters = { document_id: 3, document_name: "DUP", pending: 3, approved: 1, amended: 0, rejected: 0, total: 4 } as ReviewCounters;
    expect(progressOf(counters)).toEqual({ reviewed: 1, total: 4, pending: 3, pct: 25 });
    expect(countAfter(counters, "pending", "approved")).toMatchObject({ pending: 2, approved: 2 });
    expect(progressOf(null).pct).toBe(0);
  });
});

describe("filters and refusals", () => {
  it("reads the URL and builds the API query", () => {
    const filters = parseReviewFilters({ document: "3", file: "9", status: "pending", entity: "block", page: "14", sort: "confidence", zone: "x" });
    expect(filters).toEqual({ document: 3, file: 9, status: "pending", zone: null, entity: "block", page: 14, sort: "confidence" });
    expect(reviewQuery(filters, 200)).toMatchObject({ document_id: 3, file_id: 9, entity_type: "block", source_page: 14, sort: "confidence", offset: 200 });
    expect(reviewHrefFor(filters)).toBe("/admin/review?document=3&file=9&status=pending&entity=block&page=14&sort=confidence");
    expect(parseReviewFilters({}).sort).toBe("pending");
  });

  it("explains closed decisions plainly", () => {
    expect(explainReviewProblem({ status: 409, details: { reason: "published" } })).toMatch(/published/);
    expect(explainReviewProblem({ status: 409, details: { reason: "superseded" } })).toMatch(/newer reading/);
    expect(explainReviewProblem({ status: 0 })).toMatch(/did not answer/);
  });
});
