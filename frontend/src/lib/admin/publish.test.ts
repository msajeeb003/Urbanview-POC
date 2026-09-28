import { describe, expect, it } from "vitest";

import { blockersText, canRollBackTo, countsText, jobSteps, sizeText, stepChip, stepLabel, type PublishVersion } from "./publish";

const version = (over: Partial<PublishVersion>): PublishVersion =>
  ({
    id: 2,
    label: "2026-09-28.1",
    is_current: false,
    published_at: "2026-09-28T10:00:00Z",
    formula_version: "poc-1",
    ...over,
  }) as PublishVersion;

describe("publish page rules", () => {
  it("names the job's steps and their state", () => {
    expect(stepLabel("flip")).toBe("Switch the live map");
    expect(stepLabel("new_step")).toBe("new step");
    expect(stepChip("done")).toEqual({ tone: "ok", label: "Done" });
    expect(stepChip("running")).toEqual({ tone: "pend", label: "Running" });
    expect(stepChip("pending").label).toBe("Waiting");
    expect(jobSteps({ step: "values", steps: [{ name: "preflight", status: "done" }, { name: "values", status: "running" }] })).toHaveLength(2);
    expect(jobSteps(null)).toEqual([]);
  });

  it("offers a rollback only to an earlier version whose archive is kept (the API's guards)", () => {
    const current = version({ id: 5, is_current: true, published_at: "2026-09-29T08:00:00Z" });
    expect(canRollBackTo(version({ id: 4, published_at: "2026-09-28T08:00:00Z" }), current)).toBe(true);
    expect(canRollBackTo(current, current)).toBe(false);
    expect(canRollBackTo(version({ id: 3, archive_pruned_at: "2026-09-29T09:00:00Z" }), current)).toBe(false);
    expect(canRollBackTo(version({ id: 4 }), null)).toBe(false);
  });

  it("summarises a version and what blocks a publish", () => {
    expect(countsText({ values_carried: 1200, values_published: 84, parcel_links: 667, choropleth_cells: { far: 10, sale_price: 2 } })).toBe(
      "1,284 values · 667 parcel links · 12 heatmap cells",
    );
    expect(countsText(null)).toBe("—");
    expect([sizeText(512), sizeText(5 * 1024 * 1024), sizeText(null)]).toEqual(["1 KB", "5.0 MB", "—"]);
    expect(blockersText([])).toBeNull();
    expect(blockersText([{ document_id: 6, document_name: "DUP Novi Grad", pending: 3 }])).toBe(
      "Publishing waits for: DUP Novi Grad (3 pending)",
    );
  });
});
