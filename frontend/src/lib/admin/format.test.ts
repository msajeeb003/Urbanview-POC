import { describe, expect, it } from "vitest";

import { auditChanges, relativeTime, resolveZone, staffZoneOf, zoneName, zoneStamp } from "./format";

describe("admin table words", () => {
  it("writes times like the mock", () => {
    const now = new Date("2026-09-26T12:00:00Z");
    expect(relativeTime("2026-09-26T10:00:00Z", now)).toBe("2h ago");
    expect(relativeTime("2026-09-25T09:00:00Z", now)).toBe("Yesterday");
    expect(relativeTime("2026-09-23T12:00:00Z", now)).toBe("3 days ago");
    expect(relativeTime("2026-08-01T12:00:00Z", now)).toBe("1 Aug 2026");
    expect(zoneStamp("2026-09-26T14:05:33.123Z")).toBe("2026-09-26 14:05");
  });

  it("writes exact times on the municipality's clock and names the zone", () => {
    // the tester's order: placed 16:07 UTC, which was 18:07 in Podgorica (summer time)
    expect(zoneStamp("2026-10-02T16:07:08.820032Z", "Europe/Podgorica")).toBe("2026-10-02 18:07");
    // winter time is one hour ahead of UTC, and the date follows the zone across midnight
    expect(zoneStamp("2026-12-31T23:30:00Z", "Europe/Podgorica")).toBe("2027-01-01 00:30");
    expect(zoneName("Europe/Podgorica")).toBe("Podgorica time");
    expect(zoneName("America/New_York")).toBe("New York time");
    expect(zoneName("UTC")).toBe("UTC");
    const now = new Date("2027-02-01T12:00:00Z");
    expect(relativeTime("2026-12-31T23:30:00Z", now, "Europe/Podgorica")).toBe("1 Jan 2027");
    expect(relativeTime("2026-12-31T23:30:00Z", now)).toBe("31 Dec 2026");
  });

  it("falls back to UTC, named as UTC, when the zone is unknown", () => {
    expect(resolveZone("Europe/Podgorica")).toBe("Europe/Podgorica");
    expect(resolveZone("Mars/Olympus")).toBe("UTC");
    expect(resolveZone(null)).toBe("UTC");
    const unknown = staffZoneOf(undefined);
    expect(unknown.named("2026-10-02T16:07:00Z")).toBe("2026-10-02 16:07 UTC");
    const podgorica = staffZoneOf("Europe/Podgorica");
    expect(podgorica.name).toBe("Podgorica time");
    expect(podgorica.stamp("2026-10-02T16:07:00Z")).toBe("2026-10-02 18:07");
    expect(podgorica.named("2026-10-02T16:07:00Z")).toBe("2026-10-02 18:07 Podgorica time");
  });

  it("shows only what an audited change touched", () => {
    expect(auditChanges({ role: "reviewer", is_active: true }, { role: "admin", is_active: true })).toEqual([
      { key: "role", from: "reviewer", to: "admin" },
    ]);
    expect(auditChanges(null, { is_current: false })).toEqual([{ key: "is_current", from: "∅", to: "false" }]);
    expect(auditChanges({ a: [1] }, { a: [1] })).toEqual([]);
  });
});
