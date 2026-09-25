import { describe, expect, it } from "vitest";

import type { MunicipalityProfile } from "@/lib/api/types";
import { formatDate, formatFigure } from "@/lib/format";
import { nextSheet } from "@/lib/store";

import { docTypeLabel, documentMeta, heightText, planPhrase, zoneTypicalLine, type DocZone, type ZoneDoc } from "./panel-parts";

function doc(overrides: Partial<ZoneDoc> = {}): ZoneDoc {
  return {
    id: 2,
    name: "DUP Centar – Zona C2",
    type: "DUP",
    status: "adopted",
    status_label_en: "adopted",
    status_label_me: "usvojen",
    source: "eRegistri",
    registry_url: null,
    amends_document_id: null,
    adopted_on: null,
    covered: true,
    file_available: true,
    parcel_count: 4,
    ...overrides,
  };
}

describe("documentMeta (zone panel document list)", () => {
  it("reads like the wireframe, plus the parcels the map covers", () => {
    expect(documentMeta(doc())).toBe("source PDF · eRegistri · 4 parcels with data");
    expect(documentMeta(doc({ parcel_count: 1 }))).toBe("source PDF · eRegistri · 1 parcel with data");
  });
  it("adds the adoption date when it is known", () => {
    expect(documentMeta(doc({ adopted_on: "2019-05-12" }))).toBe(
      "source PDF · eRegistri · adopted 12 May 2019 · 4 parcels with data",
    );
  });
  it("says an adopted plan without coverage is not yet digitised", () => {
    expect(documentMeta(doc({ covered: false, parcel_count: null, file_available: false }))).toBe(
      "eRegistri · not yet digitised",
    );
  });
  it("leaves plans in progress and superseded plans without a coverage phrase", () => {
    expect(documentMeta(doc({ status: "in_progress", covered: false, parcel_count: null, file_available: false }))).toBe(
      "eRegistri",
    );
    expect(documentMeta(doc({ status: "superseded", covered: false, parcel_count: null }))).toBe("source PDF · eRegistri");
  });
});

describe("zone typical values", () => {
  const zone = (typical: DocZone["typical"]): DocZone => ({ id: 1, name: "Centar", zone_type: "mix", typical });

  it("shows height and FAR per zone spanned", () => {
    expect(zoneTypicalLine(zone({ max_height_m: 24, max_floors: 7, max_far: 3.2 }))).toBe("24 m · FAR 3.2");
    expect(zoneTypicalLine(zone({ max_floors: 5, max_far: 2 }))).toBe("5 floors · FAR 2");
    expect(zoneTypicalLine(zone({ land_use: "Residential" }))).toBe("—");
    expect(zoneTypicalLine(zone(null))).toBe("—");
  });
  it("writes the typical height from metres and floors", () => {
    expect(heightText({ max_height_m: 24, max_floors: 7 })).toBe("24 m · 7 floors");
    expect(heightText({ max_floors: 1 })).toBe("1 floor");
    expect(heightText({ max_height_m: 10.25 })).toBe("10.3 m");
    expect(heightText({})).toBeNull();
  });
});

describe("document type and status", () => {
  const profile = {
    terminology: {
      document_types: { DUP: "Detaljni urbanistički plan (detailed urban plan)", PGR: "Plan generalne regulacije" },
      document_types_en: { DUP: "Detailed urban plan" },
    },
  } as unknown as MunicipalityProfile;

  it("names the type from the profile, English first", () => {
    expect(docTypeLabel("DUP", profile)).toBe("DUP — Detailed urban plan");
    expect(docTypeLabel("PGR", profile)).toBe("PGR — Plan generalne regulacije");
    expect(docTypeLabel("XYZ", profile)).toBe("XYZ");
    expect(docTypeLabel(null, profile)).toBe("");
    expect(docTypeLabel("DUP", undefined)).toBe("DUP");
  });
  it("words the document panel's eyebrow by status", () => {
    expect(planPhrase("adopted")).toBe("adopted plan");
    expect(planPhrase("in_progress")).toBe("plan in progress");
    expect(planPhrase("superseded")).toBe("superseded plan");
  });
});

describe("formatting and the bottom sheet", () => {
  it("formats figures and dates", () => {
    expect(formatFigure(3.2, 1)).toBe("3.2");
    expect(formatFigure(55, 1)).toBe("55");
    expect(formatFigure(2.25, 1)).toBe("2.3");
    expect(formatDate("2019-05-12")).toBe("12 May 2019");
    expect(formatDate("2026-09-22")).toBe("22 Sep 2026");
    expect(formatDate("not a date")).toBeNull();
  });
  it("cycles the sheet peek → half → full → peek", () => {
    expect(nextSheet("peek")).toBe("half");
    expect(nextSheet("half")).toBe("full");
    expect(nextSheet("full")).toBe("peek");
  });
});
