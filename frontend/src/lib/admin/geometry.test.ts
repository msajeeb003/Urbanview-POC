import { describe, expect, it } from "vitest";

import type { GeometryDraft } from "@/lib/api/types";

import {
  decisionChip,
  draftSource,
  draftTitle,
  emptyGeometryText,
  explainGeometryProblem,
  geometryBlockersText,
  geometryQuery,
  issueName,
  nextPendingDraft,
  parseGeometryFilters,
  projectFeatures,
  qaChip,
} from "./geometry";

function draft(over: Partial<GeometryDraft> = {}): GeometryDraft {
  return {
    id: 7,
    layer_id: "urban_parcels",
    layer_label: "Planned urban parcels",
    origin: "vector_pdf",
    status: "staged",
    review_status: "pending",
    feature_count: 560,
    document: { id: 12, name: "UP Stara Varoš", short_code: "UP-SV", type: "UP" },
    dataset: { kind: "georef", version: "geo-12-20261001-1", status: "staged" },
    produced_by: "georef:cli",
    created_at: "2026-10-01T09:00:00Z",
    qa_status: "warn",
    qa_issues: [],
    can_approve: true,
    can_reject: true,
    ...over,
  } as GeometryDraft;
}

describe("how a draft reads", () => {
  it("names the layer and the document, then origin, run and size", () => {
    expect(draftTitle(draft())).toBe("Planned urban parcels — UP-SV");
    expect(draftTitle(draft({ document: null, dataset: { kind: "zones", version: "podgorica-zones-20261001-1", status: "staged" } }))).toBe(
      "Planned urban parcels — podgorica-zones-20261001-1",
    );
    expect(draftSource(draft())).toBe("Vector plan PDF · georeferencing geo-12-20261001-1 · 560 features");
    expect(draftSource(draft({ origin: null, dataset: null, feature_count: 1 }))).toBe("Origin not recorded · 1 feature");
  });

  it("chips say the QA and the decision", () => {
    expect(qaChip("pass")).toEqual({ tone: "ok", label: "QA passed" });
    expect(qaChip("fail")).toEqual({ tone: "rev", label: "QA fails" });
    expect(qaChip(null).label).toBe("Not checked yet");
    expect(decisionChip(draft()).label).toBe("Pending");
    expect(decisionChip(draft({ review_status: "approved" })).label).toBe("Approved — next publish");
    expect(decisionChip(draft({ status: "rejected", review_status: "rejected" })).tone).toBe("rev");
    expect(decisionChip(draft({ status: "published", review_status: null })).label).toBe("Published before review");
    expect(decisionChip(draft({ status: "superseded" })).label).toBe("Superseded");
  });

  it("issue names, the dataset's own warnings included", () => {
    expect(issueName("overlap")).toBe("Overlap");
    expect(issueName("area_deviation")).toBe("Area differs from the plan");
    expect(issueName("georef.systematic_offset")).toBe("Systematic offset (georeferencing)");
    expect(issueName("cadastre.parcels_below_1m2")).toBe("Parcels below 1m2 (cadastral import)");
  });
});

describe("the queue", () => {
  it("finds the next pending draft, wrapping around", () => {
    const list = [draft({ id: 1 }), draft({ id: 2, review_status: "approved" }), draft({ id: 3 })];
    expect(nextPendingDraft(list, 0)).toBe(2);
    expect(nextPendingDraft(list, 2)).toBe(0);
    expect(nextPendingDraft([draft({ review_status: "approved" })], 0)).toBeNull();
  });

  it("reads its filters from the URL and asks the API with them", () => {
    const filters = parseGeometryFilters({ status: "pending", origin: "manual_qgis", layer: "zones", document: "4", history: "1" });
    expect(filters).toEqual({ status: "pending", origin: "manual_qgis", layer: "zones", document: 4, history: true });
    expect(geometryQuery(filters)).toEqual({
      status: "pending",
      origin: "manual_qgis",
      layer_id: "zones",
      document_id: 4,
      include_history: true,
      limit: 200,
    });
    const junk = parseGeometryFilters({ status: "maybe", origin: "drone", layer: "roads", document: "-2" });
    expect(junk).toEqual({ status: null, origin: null, layer: null, document: null, history: false });
    expect(emptyGeometryText(junk)).toMatch(/No staged geometry waits for review/);
    expect(emptyGeometryText(filters)).toBe("No staged geometry matches these filters.");
  });

  it("says what publishing waits for and why a decision was refused", () => {
    expect(geometryBlockersText([])).toBeNull();
    const blocker = { batch_id: 1, layer_id: "zones", layer_label: "Zones" };
    expect(geometryBlockersText([blocker])).toBe("1 geometry batch waits for review");
    expect(geometryBlockersText([blocker, { ...blocker, batch_id: 2 }])).toBe("2 geometry batches wait for review");
    expect(explainGeometryProblem({ status: 409, details: { reason: "qa_failed" } })).toMatch(/checks fail/);
    expect(explainGeometryProblem({ status: 409, details: { reason: "rejected" } })).toMatch(/stage it again/);
    expect(explainGeometryProblem({ status: 403 })).toBe("Your role cannot do this.");
  });
});

describe("the preview", () => {
  const square = (x0: number, y0: number, x1: number, y1: number) => ({
    type: "Polygon",
    coordinates: [
      [
        [x0, y0],
        [x1, y0],
        [x1, y1],
        [x0, y1],
        [x0, y0],
      ],
    ],
  });

  it("fits the batch north up and keeps the features' issues and the gaps", () => {
    const { shapes, gaps } = projectFeatures(
      {
        bbox: [19.0, 42.0, 19.002, 42.001],
        features: {
          type: "FeatureCollection",
          features: [
            { type: "Feature", id: "a", geometry: square(19.0, 42.0, 19.001, 42.001), properties: { key: "a", label: "UP 1", area_m2: 9000, issues: ["overlap"] } },
            { type: "Feature", id: "b", geometry: square(19.001, 42.0, 19.002, 42.001), properties: { key: "b", label: "UP 2", area_m2: null, issues: [] } },
          ],
        },
        gaps: [[19.001, 42.0005, 3.5]],
      },
      200,
      100,
      0,
    );
    expect(shapes.map((s) => [s.key, s.label, s.issues, s.area])).toEqual([
      ["a", "UP 1", ["overlap"], 9000],
      ["b", "UP 2", [], null],
    ]);
    // north up: the first corner (south-west) sits at the bottom left; closed rings end with Z
    expect(shapes[0].d.startsWith("M")).toBe(true);
    expect(shapes[0].d.endsWith("Z")).toBe(true);
    const [firstX, firstY] = shapes[0].d.slice(1).split("L")[0].split(" ").map(Number);
    expect(firstY).toBeGreaterThan(50);
    expect(firstX).toBeLessThan(100);
    expect(gaps).toHaveLength(1);
    expect(gaps[0].m2).toBe(3.5);
    expect(gaps[0].y).toBeGreaterThan(0);
    expect(gaps[0].y).toBeLessThan(100);
  });

  it("draws nothing without an extent", () => {
    expect(projectFeatures({ bbox: null, features: { type: "FeatureCollection", features: [] }, gaps: [] }, 100, 100)).toEqual({
      shapes: [],
      gaps: [],
    });
  });
});
