import { describe, expect, it } from "vitest";

import { auditChanges, extractionLabel, liveChip, relativeTime, reviewLabel, utcStamp } from "./format";

describe("admin table words", () => {
  it("uses the wireframe's pipeline vocabulary", () => {
    expect(extractionLabel("queued")).toBe("Queued");
    expect(extractionLabel("in_progress")).toBe("In progress");
    expect(extractionLabel("done")).toBe("Done");
    expect(extractionLabel("none")).toBe("—");
    expect(reviewLabel(59.6)).toBe("60%");
    expect(reviewLabel(null)).toBe("—");
    expect(liveChip("yes")).toEqual({ tone: "ok", label: "Yes" });
    expect(liveChip("partial")).toEqual({ tone: "rev", label: "Partial" });
    expect(liveChip("no")).toEqual({ tone: "pend", label: "No" });
  });

  it("writes times like the mock", () => {
    const now = new Date("2026-09-26T12:00:00Z");
    expect(relativeTime("2026-09-26T10:00:00Z", now)).toBe("2h ago");
    expect(relativeTime("2026-09-25T09:00:00Z", now)).toBe("Yesterday");
    expect(relativeTime("2026-09-23T12:00:00Z", now)).toBe("3 days ago");
    expect(relativeTime("2026-08-01T12:00:00Z", now)).toBe("1 Aug 2026");
    expect(utcStamp("2026-09-26T14:05:33.123Z")).toBe("2026-09-26 14:05");
  });

  it("shows only what an audited change touched", () => {
    expect(auditChanges({ role: "reviewer", is_active: true }, { role: "admin", is_active: true })).toEqual([
      { key: "role", from: "reviewer", to: "admin" },
    ]);
    expect(auditChanges(null, { is_current: false })).toEqual([{ key: "is_current", from: "∅", to: "false" }]);
    expect(auditChanges({ a: [1] }, { a: [1] })).toEqual([]);
  });
});
