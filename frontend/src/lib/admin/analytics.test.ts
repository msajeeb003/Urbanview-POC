import { describe, expect, it } from "vitest";

import {
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
} from "./analytics";

const district = (over: Partial<District>): District => ({
  zone_id: 1,
  zone_name: "Centar",
  covered: true,
  events: 3,
  searches: 2,
  uncovered_searches: 0,
  selections: 1,
  sessions: 2,
  share_pct: 50,
  ...over,
});

describe("analytics page rules", () => {
  it("reads the date range of the form and sends `to` as the exclusive next day", () => {
    expect(parseRange({ from: "2026-09-01", to: "2026-09-30" })).toEqual({ from: "2026-09-01", to: "2026-09-30" });
    expect(rangeQuery({ from: "2026-09-01", to: "2026-09-30" })).toEqual({ from: "2026-09-01", to: "2026-10-01" });
    expect(rangeQuery({ from: "", to: "" })).toEqual({}); // the API's last 30 days
    expect(parseRange({ from: "yesterday", to: ["2026-09-30"] })).toEqual({ from: "", to: "2026-09-30" });
    expect(parseRange({ from: "2026-10-01", to: "2026-09-01" })).toEqual({ from: "", to: "" }); // reversed
  });

  it("labels the range, the funnel steps and the figures", () => {
    expect(rangeLabel({ from: "2026-09-01T00:00:00Z", to: "2026-10-01T00:00:00Z", days: 30 })).toBe("1 Sep 2026 – 30 Sep 2026");
    expect(stepLabel("searched_or_selected")).toBe("Searched or picked a parcel");
    expect(stepLabel("new_step")).toBe("new step");
    expect([pctText(42.5), pctText(100), pctText(null)]).toEqual(["42.5%", "100%", "—"]);
    expect([ratioText(2.25), ratioText(null)]).toEqual(["2.3", "—"]);
    expect([count(1, "session"), count(1200, "session"), count(2, "search", "searches")]).toEqual([
      "1 session",
      "1,200 sessions",
      "2 searches",
    ]);
  });

  it("names districts, says whether an adopted plan covers them, and sums the uncovered demand", () => {
    const konik = district({ zone_id: 5, zone_name: "Konik", covered: false, searches: 3, uncovered_searches: 3 });
    const nowhere = district({ zone_id: null, zone_name: null, covered: null, uncovered_searches: 1 });
    expect(districtName(konik)).toBe("Konik");
    expect(districtName(nowhere)).toBe("Outside every district");
    expect(districtChip(district({}))).toEqual({ tone: "ok", label: "Covered" });
    expect(districtChip(konik)).toEqual({ tone: "rev", label: "No adopted plan" });
    expect(districtChip(nowhere)).toBeNull();
    expect(uncoveredDemand([district({}), konik, nowhere])).toEqual({ searches: 4, districts: 1 });
  });
});
