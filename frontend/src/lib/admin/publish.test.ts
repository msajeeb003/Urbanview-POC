import { describe, expect, it } from "vitest";

import {
  blockersText,
  countsText,
  jobSteps,
  shortError,
  sizeText,
  stepChip,
  stepLabel,
} from "./publish";

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

  it("shortens a failed publish's error at a word", () => {
    expect(shortError("TileBuildError: tippecanoe exited with 1: out of memory")).toBe("TileBuildError: tippecanoe exited with 1: out of memory");
    const long = `TileBuildError: tippecanoe exited with 1: ${"layer urban_parcels failed ".repeat(10)}`;
    // 200 characters end inside the sixth "failed": the note stops at the word before it
    expect(shortError(long)).toBe(`TileBuildError: tippecanoe exited with 1: ${"layer urban_parcels failed ".repeat(5)}layer urban_parcels…`);
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
    // geometry review (0033): staged batches waiting for a decision block publishing too
    const batch = { batch_id: 4, layer_id: "urban_parcels", layer_label: "Planned urban parcels" };
    expect(blockersText([], [batch, { ...batch, batch_id: 5 }])).toBe("Publishing waits for: 2 geometry batches to review");
    expect(blockersText([{ document_id: 6, document_name: "DUP Novi Grad", pending: 3 }], [batch])).toBe(
      "Publishing waits for: DUP Novi Grad (3 pending), 1 geometry batch to review",
    );
  });
});
