import { describe, expect, it } from "vitest";

import {
  count,
  districtChip,
  districtName,
  districtNote,
  parseRange,
  pctText,
  positionText,
  rangeLabel,
  rangeQuery,
  stepLabel,
  type ZoneHits,
} from "./analytics";

const district = (over: Partial<ZoneHits>): ZoneHits => ({
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
    expect(stepLabel("parcel_resolved")).toBe("Picked a parcel");
    expect(stepLabel("order_submitted")).toBe("Placed an order");
    expect(stepLabel("paid")).toBe("Paid");
    expect(stepLabel("new_step")).toBe("new step");
    expect([pctText(42.5), pctText(100), pctText(0), pctText(null)]).toEqual(["42.5%", "100%", "0%", "—"]);
    expect(positionText({ lat: 42.46, lng: 19.281 })).toBe("42.460° N · 19.281° E");
    expect([count(1, "session"), count(1200, "session"), count(2, "search", "searches")]).toEqual([
      "1 session",
      "1,200 sessions",
      "2 searches",
    ]);
  });

  it("names districts and says whether an adopted plan covers them", () => {
    const konik = district({ zone_id: 5, zone_name: "Konik", covered: false, searches: 3, uncovered_searches: 3 });
    const nowhere = district({ zone_id: null, zone_name: null, covered: null, uncovered_searches: 1 });
    expect(districtName(konik)).toBe("Konik");
    expect(districtNote(konik)).toBeNull();
    // no district could be recorded (no position on the event): never called "outside"
    expect(districtName(nowhere)).toBe("No district recorded");
    expect(districtNote(nowhere)).toMatch(/without a recorded position/);
    // events of a district that was removed since keep its number, named as such
    const removed = district({ zone_id: 1, zone_name: null, covered: null });
    expect(districtName(removed)).toBe("Removed district #1");
    expect(districtNote(removed)).toMatch(/no longer exists/);
    expect(districtChip(removed)).toBeNull();
    expect(districtChip(district({}))).toEqual({ tone: "ok", label: "Covered" });
    expect(districtChip(konik)).toEqual({ tone: "rev", label: "No adopted plan" });
    expect(districtChip(nowhere)).toBeNull();
  });
});
