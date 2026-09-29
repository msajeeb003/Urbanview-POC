import { describe, expect, it } from "vitest";

import type { AssumptionSet } from "@/lib/api/types";

import {
  applyNote,
  checkDraft,
  dateProblem,
  diffVersions,
  draftFrom,
  emptyDraft,
  isChanged,
  parseNumber,
  previousVersion,
  rangeText,
  zoneRows,
} from "./assumptions";

const rate = (expected: number, bounds?: [number, number]) =>
  bounds
    ? { expected, low: bounds[0], high: bounds[1], kind: "absolute" as const }
    : { expected, low: expected * 0.86, high: expected * 1.15, kind: "multiplier" as const };

function version(over: Partial<AssumptionSet> & { id: number; version: number }): AssumptionSet {
  return {
    zone_id: 1,
    zone_name: "Centar",
    status: "live",
    is_current: true,
    land_rate: rate(1350),
    build_rate: rate(860),
    design_rate: rate(90),
    sale_rate: rate(2450, [2300, 2600]),
    range_low_factor: 0.86,
    range_high_factor: 1.15,
    saleable_share: null,
    source: "Realitica",
    effective_from: "2026-09-20",
    applies_from: "2026-09-20",
    created_at: "2026-09-20T08:00:00Z",
    created_by: "ops",
    ...over,
  } as AssumptionSet;
}

describe("rows", () => {
  it("groups versions per zone: live, scheduled soonest first, history newest first", () => {
    const rows = zoneRows(
      [
        { id: 2, name: "Stari Aerodrom" },
        { id: 1, name: "Centar" },
        { id: 3, name: "Konik" },
      ],
      [
        version({ id: 1, version: 1, status: "superseded", is_current: false }),
        version({ id: 5, version: 3, status: "scheduled", applies_from: "2026-10-15" }),
        version({ id: 4, version: 2, status: "live", is_current: false }),
        version({ id: 6, version: 4, status: "scheduled", applies_from: "2026-10-01" }),
        version({ id: 9, version: 1, zone_id: null, zone_name: null }),
      ],
    );
    expect(rows.map((r) => r.name)).toEqual(["Centar", "Konik", "Stari Aerodrom"]);
    const centar = rows[0];
    expect(centar.live?.id).toBe(4);
    expect(centar.scheduled.map((s) => s.id)).toEqual([6, 5]);
    expect(centar.history.map((s) => s.version)).toEqual([4, 3, 2, 1]);
    expect(rows[1].live).toBeNull(); // Konik: no figures yet
  });
});

describe("drafts", () => {
  it("start from the live version and round-trip unchanged", () => {
    const live = version({ id: 4, version: 2, saleable_share: 0.75 });
    const draft = draftFrom(live);
    expect(draft.rates.sale).toEqual({ expected: "2450", low: "2300", high: "2600" });
    expect(draft.rates.land).toEqual({ expected: "1350", low: "", high: "" });
    expect([draft.lowPct, draft.highPct, draft.saleablePct]).toEqual(["14", "15", "75"]);
    expect(isChanged(draft, live)).toBe(false);
    expect(isChanged({ ...draft, source: "Estitor" }, live)).toBe(true);
    expect(isChanged(emptyDraft(), null)).toBe(false);
  });

  it("check the API's rules and build the payload", () => {
    const draft = draftFrom(version({ id: 4, version: 2 }));
    draft.rates.build.expected = "950,5";
    draft.saleablePct = "72";
    const ok = checkDraft(draft);
    expect(ok.ok).toBe(true);
    if (!ok.ok) return;
    expect(ok.value.build_rate).toEqual({ expected: 950.5 });
    expect(ok.value.sale_rate).toEqual({ expected: 2450, low: 2300, high: 2600 });
    expect([ok.value.range_low_factor, ok.value.range_high_factor, ok.value.saleable_share]).toEqual([0.86, 1.15, 0.72]);

    const bad = draftFrom(version({ id: 4, version: 2 }));
    bad.rates.land.expected = "-5";
    bad.rates.sale.low = "2500"; // above expected 2450
    bad.rates.design.high = ""; // design has no bounds: fine
    bad.rates.build.low = "800"; // one bound only
    bad.saleablePct = "120";
    bad.source = " ";
    const result = checkDraft(bad);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors).toMatchObject({
      "land.expected": expect.any(String),
      "sale.bounds": "low ≤ expected ≤ high",
      "build.bounds": "give both bounds, or neither",
      saleablePct: expect.any(String),
      source: expect.any(String),
    });
    expect(result.errors["design.bounds"]).toBeUndefined();
  });

  it("reads numbers the way people type them", () => {
    expect(parseNumber(" 1 350 ")).toBe(1350);
    expect(parseNumber("12,5")).toBe(12.5);
    expect(Number.isNaN(parseNumber("12a"))).toBe(true);
    expect(Number.isNaN(parseNumber(""))).toBe(true);
  });
});

describe("dates", () => {
  it("accepts today or later, and says when the figures apply", () => {
    expect(dateProblem("2026-09-26", "2026-09-26")).toBeNull();
    expect(dateProblem("2026-09-25", "2026-09-26")).toMatch(/today/);
    expect(dateProblem("2036-01-01", "2026-09-26")).toMatch(/five years/);
    expect(applyNote("2026-09-26", "2026-09-26")).toMatch(/^Applies today/);
    expect(applyNote("2026-10-15", "2026-09-26")).toBe(
      "Scheduled: the panel keeps today's figures until 15 Oct 2026 (in 19 days).",
    );
  });
});

describe("history", () => {
  it("writes ranges and the diff against the previous version", () => {
    expect(rangeText(0.86, 1.15)).toBe("−14% / +15%");
    expect(rangeText(0.9, 1.1)).toBe("±10%");
    const v1 = version({ id: 1, version: 1, status: "superseded" });
    const v2 = version({ id: 4, version: 2, build_rate: rate(950), saleable_share: 0.75, applies_from: "2026-09-26" });
    expect(previousVersion([v2, v1], v2)?.id).toBe(1);
    expect(diffVersions(v1, v2)).toEqual([
      { label: "Construction €/m²", before: "€860", after: "€950" },
      { label: "Saleable share", before: "70% (default)", after: "75%" },
      { label: "Applies from", before: "20 Sep 2026", after: "26 Sep 2026" },
    ]);
  });
});
