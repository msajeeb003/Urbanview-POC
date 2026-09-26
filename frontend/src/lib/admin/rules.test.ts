import { describe, expect, it } from "vitest";

import type { ZoneParameterSet } from "@/lib/api/types";

import { checkRule, heightText, ruleDraftFrom, sourceText, staleVerification, verifiedChip } from "./rules";

const set = {
  id: 3,
  zone_id: 1,
  zone_name: "Centar",
  version: 2,
  is_current: true,
  land_use: "Mixed use",
  max_far: 3.2,
  max_site_coverage_pct: 55,
  max_height_m: 27.5,
  max_floors: 8,
  source: { document_id: 12, document_name: "DUP Centar – Zona C2", page: 14, note: "table 3", registry_url: null },
  verified_on: "2026-09-20",
  verified_by: "M. Petrović",
  created_by: "ops",
  created_at: "2026-09-20T08:00:00Z",
} as ZoneParameterSet;

describe("planning rules", () => {
  it("write the row the way the wireframe does", () => {
    expect(heightText(set)).toBe("27.5 m · 8 floors");
    expect(heightText({ max_height_m: null, max_floors: 1 })).toBe("1 floor");
    expect(sourceText(set)).toBe("DUP Centar – Zona C2 · p.14");
    expect(verifiedChip(set)).toEqual({ tone: "ok", label: "Verified", detail: "Verified 20 Sep 2026 by M. Petrović" });
    expect(verifiedChip({ verified_on: null, verified_by: null }).label).toBe("Unverified");
  });

  it("round-trip a set through the editor", () => {
    const checked = checkRule(ruleDraftFrom(set), "2026-09-26");
    expect(checked).toEqual({
      ok: true,
      value: {
        zone_id: 1,
        land_use: "Mixed use",
        max_far: 3.2,
        max_site_coverage_pct: 55,
        max_height_m: 27.5,
        max_floors: 8,
        source_document_id: 12,
        source_page: 14,
        source_note: "table 3",
        verified_on: "2026-09-20",
        verified_by: "M. Petrović",
        notes: null,
      },
    });
  });

  it("never keep an old verification on changed values", () => {
    const draft = ruleDraftFrom(set);
    expect(staleVerification(draft, set)).toBe(false);
    expect(staleVerification({ ...draft, notes: "typo" }, set)).toBe(false); // notes are not values
    expect(staleVerification({ ...draft, far: "3.4" }, set)).toBe(true);
    expect(staleVerification({ ...draft, far: "3.4", verifiedOn: "2026-09-26" }, set)).toBe(false);
    expect(staleVerification({ ...draft, far: "3.4" }, null)).toBe(false);
  });

  it("check the API's rules", () => {
    const draft = { ...ruleDraftFrom(null, 2), far: "25", floors: "4.5", page: "3", verifiedOn: "2026-10-01" };
    const result = checkRule(draft, "2026-09-26");
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.errors).toMatchObject({
      far: expect.stringMatching(/0 to 20/),
      floors: expect.stringMatching(/whole number/),
      page: "a page needs a source document",
      verifiedOn: "not in the future",
    });
    const empty = checkRule(ruleDraftFrom(null, 2), "2026-09-26");
    expect(empty.ok ? null : empty.errors.values).toMatch(/at least one/);
    const noZone = checkRule({ ...ruleDraftFrom(null), far: "2" }, "2026-09-26");
    expect(noZone.ok ? null : noZone.errors.zoneId).toBe("choose the zone");
  });
});
